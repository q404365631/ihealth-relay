"""Stdout Publisher: JSON 1-line を sys.stdout に書く Publisher (Phase 1 A3-stdout).

Unix pipe で composable な出力. 例::

    python3 -m ihealth --publisher stdout --date 2026-04-22 | jq '.step_count'

設計方針:
- **JSON は 1 行 (newline 終端)**. JSON Lines (JSONL) 形式. ``jq`` や ``awk`` が
  そのまま処理できる.
- **None フィールドは省略** (Notion / Markdown publisher と同じセマンティクス).
- **値の型と精度は :mod:`ihealth.publishers._payload` の FIELD_SPECS と統一**.
  step_count は int、距離・睡眠は 2 桁丸め float、心拍・体重は 1 桁丸め float.
- **出力先 stream は注入可能**. 既定は ``sys.stdout`` だが, テストや出力先
  リダイレクトで io.StringIO / 任意のファイルオブジェクトに差し替えられる.
- **logger は stderr に出る** (ihealth.logger の StreamHandler が既定で sys.stderr
  を使う). よって stdout には JSON 1 行だけが出る = pipe 安全.
- **BrokenPipeError は黙って終了**. ``python3 -m ihealth --publisher stdout | head``
  のような pipe で head 側が閉じたとき silent に成功扱いにする (Unix 慣習).

I/O 失敗は :class:`StdoutPublishError` に統一して raise. workflow 層が
:class:`AppError` 階層 (:class:`StdoutAppError`, exit_code=8) にラップする.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date
from typing import IO

from ihealth.models import DailyHealthData
from ihealth.publishers._payload import (
    PayloadValidationError,
    build_payload,
)


class StdoutPublishError(RuntimeError):
    """Stdout 書き込み / 値検証で発生する例外."""


def _silence_stdout_after_broken_pipe() -> None:
    """``BrokenPipeError`` 発生後に ``sys.stdout`` を ``/dev/null`` に切り替える.

    ``publish()`` で
    ``BrokenPipeError`` を catch して return しても、CPython は **プロセス終了
    時の最終 flush** で再度同じ例外を起こし、stderr に
    ``Exception ignored in: <_io.TextIOWrapper ...> BrokenPipeError`` を
    吐く. これは「silent に成功終了する」契約 (Unix 慣習) の破れ.

    ``os.dup2(/dev/null, sys.stdout.fileno())`` で標準出力 fd を /dev/null に
    付け替えれば、終了時 flush は ``write(2)`` 成功に化けて静かに終わる.
    fileno を持たない StringIO 等の注入 stream には無害化する手段がない
    (= そもそも本物の pipe ではない) ので呼び出し側で sys.stdout 限定で呼ぶ.
    """
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return  # /dev/null さえ開けない異常時は諦める (silent best-effort)
    try:
        # sys.stdout が close 済み / fileno を持たない場合 OSError
        os.dup2(devnull_fd, sys.stdout.fileno())
    except (OSError, ValueError):
        pass
    finally:
        try:
            os.close(devnull_fd)
        except OSError:
            pass


class StdoutPublisher:
    """``DailyHealthData`` を JSON 1 行として stdout (or 任意 stream) に書く.

    Args:
        target_date: 対象日. ``health_data.date`` との不一致を弾くため.
        logger: 注入ロガー. 成功ログは :func:`logging.Logger.info` で stderr 経由.
        output_stream: 書き込み先 IO. 既定 ``sys.stdout``. テストでは
            ``io.StringIO()`` を渡して捕捉する.

    Raises (publish 内):
        StdoutPublishError: target_date 不整合 / 値検証失敗 / 書き込み失敗.
    """

    def __init__(
        self,
        target_date: date,
        logger: logging.Logger,
        *,
        output_stream: "IO[str] | None" = None,
    ) -> None:
        self._target_date = target_date
        self._logger = logger
        self._stream = output_stream  # None → publish 時に sys.stdout を採用

    def publish(self, health_data: DailyHealthData) -> None:
        """1 日分の健康データを JSON 1 行として書き出す."""
        if health_data.date != self._target_date:
            raise StdoutPublishError(
                f"target_date 不整合: publisher={self._target_date} "
                f"data={health_data.date}"
            )

        try:
            payload = build_payload(health_data)
        except PayloadValidationError as exc:
            raise StdoutPublishError(str(exc)) from exc

        # ``ensure_ascii=False`` で日本語キーが将来出てきても \\uXXXX エスケープ
        # しない (今は英語キーのみだが). ``separators`` で余分な空白を削って
        # 1 行が確実にコンパクトになるよう保つ.
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"

        is_default_stream = self._stream is None
        stream = self._stream if not is_default_stream else sys.stdout
        try:
            stream.write(line)
            stream.flush()
        except BrokenPipeError:
            # `python3 -m ihealth --publisher stdout | head` 等で receive 側が
            # 閉じたケース. Unix 慣習に従い silent に成功終了 (= 例外を上げない).
            # ただし sys.stdout の場合、CPython が **プロセス終了時の最終 flush**
            # で再度 BrokenPipeError を起こし stderr に
            # "Exception ignored in: ..." を吐く. これを避けるため stdout fd
            # を /dev/null に dup2 しておく.
            #
            # 識別は ``self._stream is None`` ベース (= 既定 sys.stdout 経路で
            # only). 注入 stream (StringIO / 任意の wrapper) のときは fd 操作を
            # しない. ``is sys.stdout`` だと sys.stdout 自体が他コードで rebind
            # された場合の判定がブレるので intent ベースの ``_stream is None``
            # 判定に揃える.
            #
            # 副作用注意: 一度 silence したらこのプロセスの stdout は以降の
            # write が /dev/null に書かれる. iHealth は CLI one-shot 実行前提
            # なのでこれで OK. 長寿命プロセスに組み込む場合は dup2 前に
            # ``os.dup(sys.stdout.fileno())`` で旧 fd を退避する必要がある.
            if is_default_stream:
                _silence_stdout_after_broken_pipe()
            return
        except (OSError, ValueError, UnicodeError) as exc:
            # - OSError: disk full / 権限不正 / EBADF など
            # - ValueError: closed file への write (TextIOWrapper の挙動)
            # - UnicodeError: stdout encoding が ASCII だった等で日本語 emit 不可
            #
            # output_stream を inject 可能な公開 API なので、ここで wrap して
            # workflow 層の StdoutAppError(8) に統一する.
            raise StdoutPublishError(
                f"stdout への書き込みに失敗: {exc}"
            ) from exc

        # 成功ログは stderr 経由 (logger handler) で出る. stdout は JSON 1 行
        # のみで pipe 安全.
        # ``payload`` の key 数 - 1 (date を除く) が populated 件数.
        populated = len(payload) - 1
        self._logger.info(
            "stdout 出力成功: target=%s populated=%d", self._target_date, populated,
        )
