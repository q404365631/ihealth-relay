"""Health Auto Export の AutoSync 出力 (.hae) を検出・解凍・アーカイブする。

.hae は Apple 公開の LZFSE アルゴリズムで圧縮された UTF-8 JSON。
macOS 標準の ``/usr/bin/compression_tool`` (Xcode Command Line Tools) で解凍できる。

AutoSync は 1 メトリクス / 1 日単位にファイルを分割して吐き出すので、
以下のディレクトリレイアウトを持つ:

    <HEALTH_EXPORT_DIR>/HealthMetrics/{metric_snake}/{YYYYMMDD}.hae

本モジュールは 13 メトリクス分を順に解凍し、
``./data/health/YYYY-MM-DD/{metric_snake}.json`` にアーカイブして、
アーカイブ先パスの辞書 (``FetchResult.archived``) を返す。以降のパース処理は
アーカイブ先を正として進める（再実行時の冪等性・デバッグ容易化）。
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


#: /usr/bin/compression_tool の絶対パス。Xcode CLT 同梱なので基本は実在する。
COMPRESSION_TOOL = Path("/usr/bin/compression_tool")

#: 解凍後 JSON のサイズ上限 (bytes)。実機で最大のメトリクス (active_energy) でも
#: 1 日 14 MB 程度なので、100 MB を上限にしておけば異常検知として十分。
_MAX_DECODED_SIZE_BYTES = 100 * 1024 * 1024

#: compression_tool 1 回あたりの実行タイムアウト (秒)。
_DECOMPRESS_TIMEOUT_SEC = 60

#: macOS ``<sys/stat.h>`` の ``SF_DATALESS`` フラグ。File Provider (iCloud Drive)
#: が「メタデータだけローカルに存在し、実体は iCloud 側のみ」の placeholder
#: 状態を表す。Python の ``stat`` モジュールには定数として登録されていない
#: (BSD 拡張) ため値直書き。
#:
#: launchd 経由のバックグラウンド python3 が dataless な ``.hae`` を
#: ``compression_tool`` に読ませると、kernel が ``EDEADLK`` を返し
#: ``compression_tool`` が ``read: Resource deadlock avoided`` で returncode=1
#: する (実機 launchd 起動経由で 2026-04-28 確認)。前景プロセスが居ないと
#: File Provider は自動 materialize しないため、明示的に ``brctl download`` を
#: 叩いて事前 materialize する必要がある。
_SF_DATALESS = 0x40000000

#: ``/usr/bin/brctl`` の絶対パス。macOS 標準 (Sonoma 以降 deprecated 警告は出るが
#: 現役)。``brctl download <path>`` で File Provider に当該ファイルの
#: 同期 download を要求する。コマンド自体は async で即時 return するため、
#: 完了は ``_SF_DATALESS`` フラグ消滅で polling 判定する。
_BRCTL = Path("/usr/bin/brctl")

#: dataless 解除 (materialize) を待つ最大時間 (秒)。実機計測では 1 ファイル ~3 秒、
#: 11 メトリクス並列で同時に走らせても 5 秒以内に完了したため、60 秒あれば
#: ネットワーク遅延が混じる朝のリトライでも十分なマージンがある。
_MATERIALIZE_TIMEOUT_SEC = 60.0

#: materialize 完了 polling 間隔 (秒)。0.5 秒 × 120 ステップで 60 秒タイムアウト。
_MATERIALIZE_POLL_INTERVAL_SEC = 0.5

#: 「毎日必ず取れるはず」のメトリクス (logical key)。iPhone を携帯している限り
#: ``step_count`` は必ず 0 件を超える (iPhone 単独でも歩数計測可能)、
#: ``heart_rate`` は Apple Watch 装着前提で 1 日 1 回は拾える。
#: これが欠落している場合は iCloud 同期遅延 / AutoSync 停止を疑い、一定回数リトライする。
#:
#: **なぜ ``heart_rate_resting`` を含めないか**: Apple の仕様上、resting rate は
#: 十分な background readings が集まった日しか算出されない派生指標で、運動量が
#: 少ない日や Watch 充電忘れの日には「正常に」欠落しうる。critical に入れると
#: 正常日を毎朝リトライで待たせた挙句 SourceError で終わらせてしまう。
#: 瞑想・体重・昼寝も同様 (その日やっていないだけを区別できない) ので critical から外す。
_CRITICAL_METRICS: "frozenset[str]" = frozenset({
    "step_count",
    "heart_rate",
})

#: 主要メトリクス欠落時のリトライ間隔 (秒) / 追加試行回数 (初回含まず)。
#: Issue #5 の要件は「2 分 × 5 回」。環境変数で上書き可能 (テスト高速化用途)。
_DEFAULT_RETRY_WAIT_SEC = 120.0
_DEFAULT_RETRY_MAX = 5

#: 環境変数による上書きの安全網。``inf`` や `1e309` を受け取って ``time.sleep`` を
#: OverflowError で落とさないために、有限値かつ上限 1 時間 / 100 回までで clamp する。
_RETRY_WAIT_UPPER_BOUND_SEC = 3600.0
_RETRY_MAX_UPPER_BOUND = 100


#: 内部ロジック上の 13 メトリクスキー → AutoSync 配下の HealthMetrics/ サブディレクトリ名。
#:
#: * ``heart_rate`` 1 つから ``heart_rate_avg`` と ``heart_rate_max`` を取り出す
#: * ``mindful_minutes`` 1 つから ``mindful_sessions`` と ``mindful_minutes`` を取り出す
#: * ``body_mass_kg`` のディレクトリは ``weight_body_mass``
#:   (``body_mass_index`` は **BMI** で別物、誤って読まないよう固定)
METRIC_DIRS: "dict[str, str]" = {
    "step_count": "step_count",
    "distance_km": "walking_running_distance",
    "active_energy_kcal": "active_energy",
    "exercise_minutes": "apple_exercise_time",
    "heart_rate": "heart_rate",
    "heart_rate_resting": "resting_heart_rate",
    "oxygen_saturation": "blood_oxygen_saturation",
    "sleep_hours": "sleep_analysis",
    "mindful_minutes": "mindful_minutes",
    "body_mass_kg": "weight_body_mass",
    "body_fat_percentage": "body_fat_percentage",
}


class SourceError(RuntimeError):
    """.hae 取得・解凍で発生する例外。"""


@dataclass(frozen=True)
class FetchResult:
    """取得結果のサマリ。

    Attributes:
        target_date: 対象日付
        archive_dir: ``./data/health/YYYY-MM-DD/`` の実パス
        archived: 解凍成功メトリクス (logical key → アーカイブ済み JSON のパス)
        missing: .hae が存在しなかった / 解凍失敗したメトリクス (logical key)
    """

    target_date: date
    archive_dir: Path
    archived: "dict[str, Path]" = field(default_factory=dict)
    missing: "list[str]" = field(default_factory=list)


def _is_dataless(path: Path) -> bool:
    """``path`` の ``SF_DATALESS`` フラグが立っているかを判定する。

    ``os.stat(2)`` はメタデータ問い合わせのみで File Provider の materialize を
    トリガしない (実機確認: dataless ファイルに対して何度 stat しても
    flag は変化しない)。検査自体に副作用がないので、解凍前ガードとして安全。

    Returns:
        ``True`` なら dataless (実体未ダウンロード) で、解凍前に
        ``_ensure_materialized`` を通す必要がある。``stat`` 自体に失敗 (権限
        エラー等) した場合や ``st_flags`` を持たない OS では ``False`` を返し、
        通常フローへ戻す (= 失敗時はあえて夢を見ない)。
    """
    try:
        return bool(os.stat(path).st_flags & _SF_DATALESS)
    except (OSError, AttributeError):
        return False


def _ensure_materialized(path: Path, log: logging.Logger) -> None:
    """``path`` が dataless なら ``brctl download`` で実体化を要求し、完了を待つ。

    dataless でなければ即時 return (高速パス、検査コストは ``stat(2)`` 1 回のみ)。

    Raises:
        SourceError: 以下のいずれか。呼び出し側 (``_fetch_once`` /
        ``fetch_metric_for_date``) は ``decompress_hae`` の失敗と同じく
        "metric 単位の missing" として扱う。

        - ``brctl`` バイナリ非存在
        - ``brctl`` プロセス起動失敗 (``PermissionError`` / ``OSError``)
        - ``brctl download`` のタイムアウトまたは非ゼロ終了
        - polling 中の ``os.stat`` 失敗 (一時的な ``ENOENT`` / ``EPERM``)
        - ``_MATERIALIZE_TIMEOUT_SEC`` 秒以内にフラグが解除されなかった

    Why this exists:
        launchd が起動した python3 から dataless な ``.hae`` を ``compression_tool``
        に読ませると ``read: Resource deadlock avoided`` (``EDEADLK``) で即時失敗
        する (実機 2026-04-28 ログ確認)。バックグラウンドプロセスは前景アクセス
        ではないため、File Provider が自動 materialize してくれない。
        明示的に ``brctl download`` を叩いて事前 materialize するのが解。

    Design notes:
        - **end-to-end deadline 制**: 単一 deadline を関数頭で固定し、``brctl``
          実行 + polling の合計で ``_MATERIALIZE_TIMEOUT_SEC`` 秒を超えない。
          以前は subprocess に 60s + polling に新たに 60s = 最悪 120s/ファイル
          になっていて、11 メトリクス逐次なら 20 分以上ぶら下がる可能性があった。
        - **polling 中は ``os.stat`` を直接呼ぶ**: ``_is_dataless`` の
          ``OSError → False`` 既定値に頼ると、一時的な ``ENOENT`` / ``EPERM``
          が「materialize 完了」と誤解釈され、後続 ``compression_tool`` 側で
          診断の濁った失敗が起きる。polling 用途では失敗を ``SourceError`` で
          そのまま明示する。
        - **``subprocess.run`` の ``OSError`` も捕捉**: ``_BRCTL.exists()`` を
          通過しても、launchd 経由で TCC が deny した場合などに ``PermissionError``
          が ``execve`` 段階で発生しうる。``SourceError`` に包んで上位の
          ``try/except SourceError`` フローへ合流させる。
    """
    if not _is_dataless(path):
        return
    if not _BRCTL.exists():
        raise SourceError(
            f"{_BRCTL} が見つかりません。dataless な .hae の materialize ができません: "
            f"{path}"
        )
    log.info("dataless 検出 → brctl で materialize 要求: %s", path)

    deadline = time.monotonic() + _MATERIALIZE_TIMEOUT_SEC

    # subprocess には残り時間を渡す。最低 0.1 秒を保証して即時タイムアウトを防ぐ
    # (deadline = now ぴったりだと timeout=0 で TimeoutExpired が即発火するため)。
    brctl_timeout = max(0.1, deadline - time.monotonic())
    try:
        result = subprocess.run(
            [str(_BRCTL), "download", str(path)],
            capture_output=True,
            timeout=brctl_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SourceError(
            f"brctl download がタイムアウト ({brctl_timeout:.1f} 秒): path={path}"
        ) from exc
    except OSError as exc:
        # _BRCTL.exists() を通過しても execve 段階で PermissionError などが
        # 出ることがある (launchd 経由で TCC が deny したケース等)。
        raise SourceError(
            f"brctl download を起動できません: path={path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise SourceError(
            f"brctl download が失敗 (returncode={result.returncode}): "
            f"path={path} stderr={stderr!r}"
        )

    # brctl は async で帰ってくるので、SF_DATALESS フラグ解除を polling で待つ。
    # ここでは _is_dataless 経由ではなく os.stat を直接呼び、OSError を
    # SourceError に変換する (一時 ENOENT/EPERM を「materialize 完了」と
    # 誤判定しないため)。
    while True:
        try:
            current_flags = os.stat(path).st_flags
        except OSError as exc:
            raise SourceError(
                f"materialize 状態確認に失敗: path={path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        except AttributeError:
            # 非 BSD 系 (Linux CI 等) で st_flags が無い → 通常はここに到達しない
            # (_is_dataless が冒頭で False を返して関数早期 return しているため)
            return

        if not (current_flags & _SF_DATALESS):
            log.debug("materialize 完了: %s", path)
            return

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_MATERIALIZE_POLL_INTERVAL_SEC, remaining))

    raise SourceError(
        f"materialize がタイムアウト ({_MATERIALIZE_TIMEOUT_SEC:.0f} 秒) しました: "
        f"path={path} (iCloud 同期遅延または File Provider の異常を確認してください)"
    )


def decompress_hae(src: Path, dst: Path) -> None:
    """``src`` の .hae を LZFSE 解凍して ``dst`` に書き出す。

    親ディレクトリは自動作成。既存 ``dst`` は上書きする（冪等）。

    Raises:
        SourceError: ``compression_tool`` が見つからない、または解凍に失敗した場合。
    """
    if not COMPRESSION_TOOL.exists():
        raise SourceError(
            f"{COMPRESSION_TOOL} が見つかりません。"
            "Xcode Command Line Tools をインストールしてください "
            "(xcode-select --install)"
        )

    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [
                str(COMPRESSION_TOOL),
                "-decode",
                "-a", "lzfse",
                "-i", str(src),
                "-o", str(dst),
            ],
            capture_output=True,
            timeout=_DECOMPRESS_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        # タイムアウト時は中途半端な dst を残さない
        dst.unlink(missing_ok=True)
        raise SourceError(
            f"compression_tool がタイムアウトしました ({_DECOMPRESS_TIMEOUT_SEC} 秒): "
            f"src={src}"
        ) from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise SourceError(
            f"compression_tool が失敗しました (returncode={result.returncode}): "
            f"src={src} dst={dst} stderr={stderr!r}"
        )
    # 解凍後サイズが異常なら切り捨て (実機で 14 MB 程度までは正常)
    try:
        size = dst.stat().st_size
    except FileNotFoundError as exc:
        raise SourceError(f"解凍後ファイルが見つかりません: {dst}") from exc
    if size > _MAX_DECODED_SIZE_BYTES:
        dst.unlink(missing_ok=True)
        raise SourceError(
            f"解凍後サイズが上限を超えました "
            f"({size / 1024 / 1024:.1f} MB > "
            f"{_MAX_DECODED_SIZE_BYTES / 1024 / 1024:.0f} MB): src={src}"
        )


def _hae_filename(target_date: date) -> str:
    """``YYYYMMDD.hae`` 形式のファイル名を返す。"""
    return target_date.strftime("%Y%m%d") + ".hae"


def fetch_all_metrics(
    health_export_dir: Path,
    target_date: date,
    archive_root: Path,
    logger: "logging.Logger | None" = None,
    retry_on_critical_missing: bool = True,
) -> FetchResult:
    """``target_date`` の 13 メトリクス分の .hae を解凍してアーカイブする。

    主要メトリクス (``_CRITICAL_METRICS``) が 1 件でも欠落していれば
    ``_RETRY_WAIT_SEC`` 秒待って最大 ``_RETRY_MAX`` 回リトライする
    (launchd で 07:00 起動した時点で iCloud 同期が追いついていないケースに備える)。
    瞑想・体重・昼寝などの欠落はリトライ対象外 (その日やっていない可能性と区別不能)。

    Args:
        health_export_dir: AutoSync ルート (``.../Documents/AutoSync``)
        target_date: 対象日付
        archive_root: ``./data/health/`` 相当のアーカイブルート
        logger: ロガー (省略時はモジュールロガー)
        retry_on_critical_missing: ``False`` で 1 回だけスキャンしてリトライしない
            (テスト / `--once` 相当の挙動)。

    Raises:
        SourceError: ``HealthMetrics`` ディレクトリそのものが存在しない場合、
            または全 retry 後も主要メトリクスが欠落している / 1 件も解凍できない場合。
            個別 (非主要) メトリクスの欠落は ``missing`` に入るだけで例外にしない。
    """
    log = logger or logging.getLogger(__name__)
    wait_sec, max_retries = _retry_settings_from_env()

    last_result: "FetchResult | None" = None
    attempts = 1 + max_retries if retry_on_critical_missing else 1
    for attempt in range(1, attempts + 1):
        last_result = _fetch_once(health_export_dir, target_date, archive_root, log)

        critical_missing = sorted(_CRITICAL_METRICS & set(last_result.missing))
        archive_empty = not last_result.archived

        if not critical_missing and not archive_empty:
            # 主要メトリクスが全部揃っていて、かつ 1 件以上 archive されている → 成功
            log.info(
                "fetch 完了: target=%s archived=%d/%d missing=%s",
                target_date, len(last_result.archived), len(METRIC_DIRS),
                last_result.missing,
            )
            return last_result

        reason = (
            f"主要メトリクス欠落 ({critical_missing})"
            if critical_missing else "1 件も解凍できず"
        )
        if attempt >= attempts:
            break  # リトライ上限に到達 → 下の SourceError raise へ
        log.warning(
            "%s: %.0f 秒後にリトライ (attempt %d/%d)",
            reason, wait_sec, attempt, attempts,
        )
        time.sleep(wait_sec)

    # 最終失敗: 明示的な SourceError で通知 (Issue #4 結線で Reminders に飛ぶ)。
    # メッセージは notify_failure の body にそのまま使われるので、運用者が
    # 「何が起きた / どれだけ試した / 次どこを見るか」を 1 箇所で判断できるよう濃くする。
    if last_result is not None:
        final_critical = sorted(_CRITICAL_METRICS & set(last_result.missing))
        archived_count = len(last_result.archived)
        missing_all = last_result.missing
    else:
        final_critical = sorted(_CRITICAL_METRICS)
        archived_count = 0
        missing_all = sorted(METRIC_DIRS.keys())

    if archived_count == 0:
        raise SourceError(
            f"対象日 {target_date} の .hae を 1 件も取得できませんでした "
            f"({attempts} 回試行 / wait={wait_sec:.0f}s): "
            f"metrics_root={health_export_dir / 'HealthMetrics'} "
            f"missing={missing_all} "
            "(iCloud 同期遅延または Health Auto Export の AutoSync 設定を確認してください)"
        )
    raise SourceError(
        f"対象日 {target_date} の主要メトリクスが {attempts} 回の試行でも到着しませんでした "
        f"(wait={wait_sec:.0f}s): "
        f"critical_missing={final_critical} "
        f"archived={archived_count}/{len(METRIC_DIRS)} "
        f"metrics_root={health_export_dir / 'HealthMetrics'}"
    )


def fetch_metric_for_date(
    *,
    health_export_dir: Path,
    metric_dir_name: str,
    target_date: date,
    archive_root: Path,
    logger: "logging.Logger | None" = None,
) -> "Path | None":
    """指定 1 メトリクスの当日 ``.hae`` を解凍してアーカイブ先の Path を返す。

    全 13 メトリクス走査の ``fetch_all_metrics`` と違い、1 メトリクスだけを
    ad-hoc に取りたいとき (例: Issue #14 の前日 ``sleep_analysis`` 併読) に使う。

    Args:
        health_export_dir: AutoSync ルート (``.../Documents/AutoSync``)
        metric_dir_name: ``HealthMetrics/`` 配下のディレクトリ名
            (例: ``sleep_analysis``)。``METRIC_DIRS.values()`` の値。
        target_date: 対象日付
        archive_root: アーカイブルート (通常 ``./data/health``)
        logger: ロガー (省略時はモジュールロガー)

    Returns:
        アーカイブ済み JSON の Path。``.hae`` が存在しない / 解凍失敗なら ``None``。
        例外は投げない (呼び出し側で graceful fallback できるように)。
    """
    log = logger or logging.getLogger(__name__)
    hm_root = health_export_dir / "HealthMetrics"
    # 事前条件: HealthMetrics ディレクトリの存在だけは明示的に確認する
    if not hm_root.is_dir():
        log.warning("HealthMetrics ディレクトリが見つかりません: %s", hm_root)
        return None
    hm_root_resolved = hm_root.resolve(strict=True)

    src_path = hm_root / metric_dir_name / _hae_filename(target_date)
    if not src_path.exists():
        log.debug(
            "metric %s の .hae が見つかりません (date=%s): %s",
            metric_dir_name, target_date, src_path,
        )
        return None
    try:
        resolved = src_path.resolve(strict=True)
        resolved.relative_to(hm_root_resolved)
    except (FileNotFoundError, ValueError) as exc:
        log.error(
            "metric %s のパス検証に失敗 (date=%s): %s",
            metric_dir_name, target_date, exc,
        )
        return None
    if not resolved.is_file():
        log.error(
            "metric %s は通常ファイルではありません: %s",
            metric_dir_name, resolved,
        )
        return None

    archive_dir = archive_root / target_date.isoformat()
    archive_dir.mkdir(parents=True, exist_ok=True)
    dst = archive_dir / f"{metric_dir_name}.json"
    try:
        _ensure_materialized(resolved, log)
        decompress_hae(resolved, dst)
    except SourceError as exc:
        log.error(
            "metric %s の解凍に失敗しました (date=%s): %s",
            metric_dir_name, target_date, exc,
        )
        return None
    return dst


def _fetch_once(
    health_export_dir: Path,
    target_date: date,
    archive_root: Path,
    log: logging.Logger,
) -> FetchResult:
    """1 スキャン分の `.hae` 解凍 + アーカイブ処理 (リトライなし)。

    全滅 (archived 空) でも例外を出さずに FetchResult を返す
    (上位層の ``fetch_all_metrics`` で retry / 最終判定する)。
    """
    metrics_root = health_export_dir / "HealthMetrics"
    if not metrics_root.is_dir():
        raise SourceError(
            f"HealthMetrics ディレクトリが見つかりません: {metrics_root}"
        )
    # path traversal 耐性: 以降の検証で realpath を使うため基準パスを固定
    metrics_root_resolved = metrics_root.resolve(strict=True)

    archive_dir = archive_root / target_date.isoformat()
    archive_dir.mkdir(parents=True, exist_ok=True)

    archived: "dict[str, Path]" = {}
    missing: "list[str]" = []
    hae_name = _hae_filename(target_date)

    for logical_key, dir_name in METRIC_DIRS.items():
        hae_path = metrics_root / dir_name / hae_name
        if not hae_path.exists():
            log.warning(
                "metric %s (dir=%s) の .hae が見つかりません: %s",
                logical_key, dir_name, hae_path,
            )
            missing.append(logical_key)
            continue

        # symlink 等で metrics_root の外へ飛び出すケースを弾く
        try:
            resolved = hae_path.resolve(strict=True)
            resolved.relative_to(metrics_root_resolved)
        except (FileNotFoundError, ValueError) as exc:
            log.error(
                "metric %s のパス検証に失敗しました (symlink による逸脱?): %s",
                logical_key, exc,
            )
            missing.append(logical_key)
            continue
        if not resolved.is_file():
            log.error(
                "metric %s は通常ファイルではありません: %s",
                logical_key, resolved,
            )
            missing.append(logical_key)
            continue

        out_path = archive_dir / f"{dir_name}.json"
        try:
            _ensure_materialized(resolved, log)
            decompress_hae(resolved, out_path)
        except SourceError as exc:
            log.error("metric %s の解凍に失敗しました: %s", logical_key, exc)
            missing.append(logical_key)
            continue

        archived[logical_key] = out_path
        log.debug(
            "metric %s: %s -> %s (size=%d bytes)",
            logical_key, hae_path.name, out_path, out_path.stat().st_size,
        )

    return FetchResult(
        target_date=target_date,
        archive_dir=archive_dir,
        archived=archived,
        missing=missing,
    )


def _retry_settings_from_env() -> "tuple[float, int]":
    """環境変数 ``IHEALTH_RETRY_WAIT_SEC`` / ``IHEALTH_RETRY_MAX`` で上書き可能。

    不正値 (非数値 / 負数 / NaN / ±Infinity / 巨大値) はデフォルトに戻す
    (launchd で誤設定が入っても運用が止まらないようにする)。
    有限値は ``_RETRY_WAIT_UPPER_BOUND_SEC`` / ``_RETRY_MAX_UPPER_BOUND`` で clamp する。

    Returns:
        ``(wait_sec, max_retries)`` のタプル。
    """
    wait_sec = _DEFAULT_RETRY_WAIT_SEC
    max_retries = _DEFAULT_RETRY_MAX
    raw_wait = os.environ.get("IHEALTH_RETRY_WAIT_SEC")
    if raw_wait:
        try:
            parsed = float(raw_wait)
        except ValueError:
            parsed = None
        # NaN / ±Infinity は time.sleep で OverflowError / ValueError を起こすので弾く
        if parsed is not None and math.isfinite(parsed) and parsed >= 0:
            wait_sec = min(parsed, _RETRY_WAIT_UPPER_BOUND_SEC)
    raw_max = os.environ.get("IHEALTH_RETRY_MAX")
    if raw_max:
        try:
            parsed_int = int(raw_max)
        except ValueError:
            parsed_int = None
        if parsed_int is not None and 0 <= parsed_int <= _RETRY_MAX_UPPER_BOUND:
            max_retries = parsed_int
    return wait_sec, max_retries
