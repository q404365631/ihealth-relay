"""Markdown Publisher: Obsidian 互換 frontmatter YAML + body 出力 (Phase 1 A3-md).

外部依存ゼロ (``pyyaml`` 等を使わず標準ライブラリのみ) で、``DailyHealthData``
を 1 日 1 ファイルの Markdown に書き出す Publisher.

設計方針:
- **出力先 ``output_dir`` は iHealth 専有のディレクトリ前提**. 同じ日付ファイルを
  毎回上書き (``os.replace`` でアトミック) する. ユーザーは Obsidian 側で
  dataview / templater から本ディレクトリを query する想定で、ここに手動編集を
  加える運用は前提にしない. (将来 v0.2.0 で「frontmatter 部分更新 + body 保持」
  をサポートする可能性があるが、v0.1.0 では over-engineering を避ける.)
- **frontmatter キーは ``DailyHealthData`` のフィールド名 (英語 snake_case)**.
  Notion publisher の Japanese 表示名とは独立した「機械可読層」に位置付ける.
  Obsidian dataview / templater から ``where step_count > 10000`` のように
  query するのが自然.
- **値は ``DailyHealthData`` の自然単位を維持** (``sleep_hours`` は時間,
  ``distance_km`` は km, ``body_mass_kg`` は kg). Notion publisher は
  別名の Notion DB 互換のため時間→分換算しているが, Markdown 側はそれを引き継
  がない.
- **``None`` フィールドは frontmatter から省略**. Notion publisher と同じ
  「測っていない日は触らない」セマンティクス.
- **YAML 1.2 minimal subset を自前 emit**. ``pyyaml`` を使わない理由は
  プロジェクト全体の「外部依存ゼロ」原則 (CLAUDE.md). 出力する scalar は
  ``int`` / ``float`` / ``date`` のみで、quoting が必要な文字列は今のところ
  emit しない (将来追加するなら quote ロジックを足す).
- **アトミック書き込み**: ``{path}.tmp`` に書いてから ``os.replace``. 書き込み
  途中で kill されても旧ファイルが残る (= 部分書きの corruption が無い).

I/O 失敗は :class:`MarkdownPublishError` に統一して raise する. workflow 層が
これを :class:`AppError` 階層 (将来追加予定の ``MarkdownAppError`` 相当) に
ラップして CLI の終了コード + Reminders 通知に乗せる.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from datetime import date
from pathlib import Path

from ihealth.models import DailyHealthData
from ihealth.publishers import _payload
from ihealth.publishers._payload import PayloadValidationError

# 後方互換: 旧名 ``MARKDOWN_FIELD_SPECS`` を _payload.FIELD_SPECS の alias として
# export する (test ファイル等の import 互換維持). A2 phase で全面 rename を検討.
MARKDOWN_FIELD_SPECS = _payload.FIELD_SPECS


def _convert_field(field_name: str, raw: object, spec: "_payload._FieldSpec") -> object:
    """``_payload.convert_field`` を呼び ``MarkdownPublishError`` に rewrap.

    Markdown publisher の API 互換 (workflow.run_pipeline は本クラスのエラーだけ
    catch する) を維持するため、共通レイヤの ``PayloadValidationError`` を
    ここで Markdown 系に変換する.
    """
    try:
        return _payload.convert_field(field_name, raw, spec)
    except PayloadValidationError as exc:
        raise MarkdownPublishError(str(exc)) from exc


_logger = logging.getLogger(__name__)


class MarkdownPublishError(RuntimeError):
    """Markdown 書き込み / 値検証で発生する例外.

    Phase 1 A3-stdout で publisher 共通の :class:`PayloadValidationError` を
    導入したが、本 publisher はそれをそのまま **本クラスに rewrap** することで
    呼び出し側 API の後方互換 (workflow.run_pipeline は MarkdownPublishError
    だけ catch する) を維持する.

    workflow 層で :class:`AppError` 階層にラップする想定.
    """


def _coerce_int(field_name: str, raw: object) -> int:
    """``_payload.coerce_int`` の rethrow ラッパ (旧 import への後方互換)."""
    from ihealth.publishers._payload import coerce_int
    try:
        return coerce_int(field_name, raw)
    except PayloadValidationError as exc:
        raise MarkdownPublishError(str(exc)) from exc


def _coerce_float(field_name: str, raw: object, ndigits: int) -> float:
    from ihealth.publishers._payload import coerce_float
    try:
        return coerce_float(field_name, raw, ndigits)
    except PayloadValidationError as exc:
        raise MarkdownPublishError(str(exc)) from exc


def _format_scalar(value: object) -> str:
    """YAML scalar として安全に文字列化する.

    対応する型は ``int`` / ``float`` のみ (frontmatter で出すのは数値のみ).
    将来 ``str`` や ``list`` を出す必要が出たら quoting ロジックを追加する.

    - ``int``: ``str(v)`` (``True``/``False`` は ``isinstance(int)`` で True に
      なるので明示的に弾く)
    - ``float``: ``str(v)``. NaN / ±Infinity は YAML では ``.nan`` / ``.inf``
      になるが, Obsidian dataview の数値 query で predictable に動く保証が
      乏しいため fail-fast (= 集計バグの早期検知).
    """
    if isinstance(value, bool):
        # bool is subclass of int — must check before int branch.
        raise TypeError(
            "MarkdownPublisher は bool を frontmatter scalar として emit しません"
        )
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MarkdownPublishError(
                f"frontmatter に NaN/Inf を出力できません: {value!r}"
            )
        return repr(value)
    raise TypeError(f"unsupported scalar type: {type(value).__name__}")


def render_frontmatter(health_data: DailyHealthData) -> str:
    """``DailyHealthData`` を Obsidian 互換 YAML frontmatter に変換する.

    出力例 (``step_count`` と ``sleep_hours`` だけ値ありの場合)::

        ---
        date: 2026-04-22
        step_count: 12345
        sleep_hours: 7.23
        ---

    - ``date`` は必須として常に先頭に出す.
    - 他フィールドは :data:`MARKDOWN_FORMATTERS` の **iteration order** で出力.
      Python 3.7+ の dict は挿入順を保つので、定義順 (= ``DailyHealthData`` の
      宣言順とほぼ同じ) で安定して並ぶ.
    - ``None`` のフィールドは行ごと省略 (Notion publisher と同じセマンティクス).
    - 末尾は ``---\\n`` で必ず改行終わり (Markdown body をそのまま続けられるよう).

    Raises:
        MarkdownPublishError: ``MARKDOWN_FORMATTERS`` の lambda が値変換に失敗
            した場合 (bool 検出, NaN/Inf 等).
    """
    lines = ["---", f"date: {health_data.date.isoformat()}"]
    for field_name, spec in MARKDOWN_FIELD_SPECS.items():
        raw = getattr(health_data, field_name, None)
        if raw is None:
            continue
        # _convert_field は publisher 共通の PayloadValidationError を raise.
        # MarkdownPublisher の API 互換のため MarkdownPublishError に rewrap.
        try:
            converted = _convert_field(field_name, raw, spec)
        except PayloadValidationError as exc:
            raise MarkdownPublishError(str(exc)) from exc
        try:
            scalar = _format_scalar(converted)
        except MarkdownPublishError:
            raise
        except (TypeError, ValueError) as exc:
            # _convert_field の出力は int/float 限定なので _format_scalar が
            # ここで失敗するのは内部矛盾. メッセージに field_name を入れて
            # debug しやすくする.
            raise MarkdownPublishError(
                f"フィールド {field_name!r} の scalar 化に失敗: "
                f"converted={converted!r} ({exc})"
            ) from exc
        lines.append(f"{field_name}: {scalar}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def render_body(health_data: DailyHealthData) -> str:
    """frontmatter の後ろに付ける Markdown 本体. v0.1.0 は最小ヘッダのみ.

    Obsidian で開いたとき H1 の日付があると識別しやすいので残す.
    将来 (v0.2.0+) で「summary 表 + 折りたたみ section」等を追加する余地.
    """
    return f"# {health_data.date.isoformat()} 健康データ\n"


class MarkdownPublisher:
    """``DailyHealthData`` を Markdown ファイル 1 つに書き出す Publisher.

    Args:
        output_dir: 書き出し先ディレクトリ. 存在しなければ ``mkdir(parents=True)``
            で作成する. iHealth 専有のディレクトリを推奨 (= ユーザー手動編集を
            行うディレクトリにこの publisher を向けない).
        target_date: 対象日. ファイル名 ``{YYYY-MM-DD}.md`` に使う.
        logger: 注入ロガー.

    Raises (publish 内):
        MarkdownPublishError: ディレクトリ作成失敗 / 書き込み失敗 / 値変換失敗
            / health_data.date と target_date が不一致.
    """

    def __init__(
        self,
        output_dir: Path,
        target_date: date,
        logger: logging.Logger,
    ) -> None:
        self._output_dir = output_dir
        self._target_date = target_date
        self._logger = logger

    def publish(self, health_data: DailyHealthData) -> None:
        """1 日分のヘルスデータを Markdown ファイルに書き出す.

        既存ファイルがあれば **完全上書き** (atomic). 部分更新はしない.
        """
        if health_data.date != self._target_date:
            raise MarkdownPublishError(
                f"target_date 不整合: publisher={self._target_date} "
                f"data={health_data.date}"
            )

        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MarkdownPublishError(
                f"出力ディレクトリ作成に失敗: {self._output_dir} ({exc})"
            ) from exc

        path = self._output_dir / f"{self._target_date.isoformat()}.md"
        content = render_frontmatter(health_data) + "\n" + render_body(health_data)

        # アトミック書き込み: 同一ディレクトリに **ユニークな tmp ファイル**
        # (tempfile.mkstemp) で書いてから ``os.replace`` で本番 path に rename.
        #
        # 重要: 固定の ``{path}.tmp`` を共有すると、同じ日付を複数プロセスが
        # 同時に publish したとき (launchd の重複起動 / 手動再実行 etc.) tmp に
        # **両プロセスが上書きしながら書き込む** ため、最終ファイルが両者の
        # 断片を含む corruption になる.
        # ``mkstemp`` は ``mkstemp(3)`` semantics で他と衝突しない名前を返すので
        # 各プロセスが自分の tmp に独立して書き、``os.replace`` 時点で
        # "last writer wins" 意味論で確定する.
        #
        # ``os.fsync`` は kernel buffer をディスクに flush し、kill / power loss
        # で「rename は完了したが内容が空」を防ぐ. macOS の APFS / ext4 等は
        # rename が durable でも先行 write は durable でないことがあるため必須.
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._output_dir),
                prefix=f".{path.name}.",
                suffix=".tmp",
            )
        except OSError as exc:
            raise MarkdownPublishError(
                f"Markdown 一時ファイル作成に失敗: {self._output_dir} ({exc})"
            ) from exc
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
        except OSError as exc:
            # 失敗時 tmp が残ったら掃除 (best-effort, 失敗は無視 = 元例外を保つ)
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise MarkdownPublishError(
                f"Markdown 書き込みに失敗: {path} ({exc})"
            ) from exc

        # 親ディレクトリの directory entry を fsync.
        # ``os.replace`` はファイル inode の指し変えだが、その変更を含む directory
        # entry は **親ディレクトリの inode に書かれている**. 親 dir を fsync しない
        # と sudden power loss で rename が永続化されず、更新後ファイルが消えたり
        # 旧版に戻る可能性がある (POSIX durability 要件).
        # macOS の APFS / Linux の ext4/xfs/btrfs では特に重要.
        # ``O_DIRECTORY`` は POSIX 拡張で macOS / Linux で有効. Windows は対象外.
        try:
            dir_fd = os.open(
                str(self._output_dir),
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
        except OSError as exc:
            # 親ディレクトリを開けない (削除された等) のはレア. fsync を諦めて
            # warning ログに留める. ファイル本体の write/replace は既に成功している.
            self._logger.warning(
                "親ディレクトリを開けず directory fsync をスキップ: %s (%s)",
                self._output_dir, exc,
            )
        else:
            try:
                os.fsync(dir_fd)
            except OSError as exc:
                # macOS の APFS / NFS など fsync(dirfd) が EINVAL になる FS がある.
                # warning にとどめてユーザーに可視化する (= 致命扱いではない).
                self._logger.warning(
                    "directory fsync 失敗 (POSIX 非互換 FS の可能性): %s (%s)",
                    self._output_dir, exc,
                )
            finally:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass

        populated = sum(
            1 for f in MARKDOWN_FIELD_SPECS
            if getattr(health_data, f) is not None
        )
        self._logger.info(
            "Markdown 出力成功: %s (populated=%d/%d)",
            path, populated, len(MARKDOWN_FIELD_SPECS),
        )
