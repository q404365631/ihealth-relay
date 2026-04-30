"""本体パイプラインとコンポーネント組み立て (Issue #16)。

``__main__.py`` はここを呼ぶだけに縮退させ、**引数処理 + AppError 例外マッピング**
に責務を絞る。本モジュールは:

- ``Publisher`` プロトコル: Notion 書き込み (実装) / dry-run (No-op) を差し替え可能に
- ``PipelineDeps``: fetch / parse / publisher を依存性注入できる dataclass
- ``run_pipeline(config, target_date, deps, logger)``: 本体実行
- ``build_default_deps(config, dry_run, logger)``: 本番用の既定 deps を組む factory

設計ポリシー:
- parser / source / notion / notifier への直接結合を composition root に閉じる
- 例外は ``AppError`` に統一して CLI 層 (``__main__``) に送る
- ``--dry-run`` は ``Publisher`` の実装差し替えで表現 (フラグを run_pipeline に露出させない)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from ihealth.config import Config
from ihealth.errors import (
    ConfigAppError,
    HealthExportDirError,
    MarkdownAppError,
    NotionAppError,  # noqa: F401 — 互換 re-export 用 (テスト側で workflow から取る)
    SlackAppError,
    SourceAppError,
    SQLiteAppError,
    StdoutAppError,
)
from ihealth.models import DailyHealthData
# Phase 1 A2: Publisher Protocol / DryRunPublisher / NotionPublisher を
# publishers/ パッケージに集約. ``ihealth.workflow.{Publisher, DryRunPublisher,
# NotionPublisher}`` の旧 import 経路は本モジュールの re-export で互換維持.
from ihealth.publishers.base import Publisher  # noqa: F401 — re-export
from ihealth.publishers.dryrun import DryRunPublisher  # noqa: F401 — re-export
from ihealth.publishers.markdown import (
    MarkdownPublishError,
    MarkdownPublisher,
)
from ihealth.publishers.notion import (
    PROPERTY_MAP,  # noqa: F401 — 互換 re-export
    NotionClient,
    NotionError,  # noqa: F401 — 互換 re-export
    NotionPublisher,
    build_properties,  # noqa: F401 — 互換 re-export
)
from ihealth.publishers.slack import (
    SlackPublishError,
    SlackPublisher,
)
from ihealth.publishers.sqlite import (
    SQLitePublishError,
    SQLitePublisher,
)
from ihealth.publishers.stdout import (
    StdoutPublishError,
    StdoutPublisher,
)
from ihealth.parser import ParseContext, parse_all
from ihealth.source import (
    FetchResult,
    SourceError,
    fetch_all_metrics,
    fetch_metric_for_date,
)


#: ``--publisher`` で受け付ける publisher 種別 (Phase 1 A3-md / stdout / sqlite / slack で拡張).
#:
#: - ``notion``: Notion API へ書き込み (既定 / 既存運用)
#: - ``markdown``: Obsidian 互換 Markdown を ``MARKDOWN_OUTPUT_DIR`` に出力
#: - ``stdout``: JSON 1 行を sys.stdout に書き出す (Unix pipe で composable)
#: - ``sqlite``: ローカル SQLite DB に 1 日 1 行 UPSERT
#: - ``slack``: Incoming Webhook で日次サマリを Slack に投稿
#: - ``dryrun``: Notion publisher の組み立て結果をログ出力するだけ (HTTP 不発射)
#:
#: ``--dry-run`` フラグが指定されたら publisher 選択にかかわらず ``dryrun`` で上書き.
PUBLISHER_KINDS = ("notion", "markdown", "stdout", "sqlite", "slack", "dryrun")


#: sleep_analysis の前日併読で参照する HealthMetrics サブディレクトリ名 (Issue #14)
_SLEEP_ANALYSIS_METRIC_DIR = "sleep_analysis"


# Phase 1 A2: Publisher / DryRunPublisher / NotionPublisher は
# ihealth.publishers.{base,dryrun,notion} に集約しました.
# ``ihealth.workflow`` からの import は file 先頭の re-export で引き続き利用可.


FetcherFn = Callable[..., FetchResult]
ParserFn = Callable[..., DailyHealthData]


@dataclass
class PipelineDeps:
    """``run_pipeline`` に差し込む依存性。テスト時はダミーに差し替え可能。

    ``fetcher`` と ``parser`` は signature 互換で差し込む (テスト用にモック関数を使う)。
    ``publisher`` は :class:`Publisher` プロトコル実装。
    ``sleep_payload_builder`` は Issue #14 の「前日併読 merge」処理。ここを注入可能に
    することで、テストで tempdir ベースのアーカイブを使えるようにする。
    """

    fetcher: FetcherFn
    parser: ParserFn
    publisher: Publisher
    sleep_payload_builder: "Callable[[Config, FetchResult, date, logging.Logger], dict[str, dict]]"


def run_pipeline(
    config: Config,
    target_date: date,
    deps: PipelineDeps,
    logger: logging.Logger,
) -> DailyHealthData:
    """本体パイプライン: 検証 → fetch → parse → publish。

    すべての失敗は ``AppError`` サブクラスに統一して raise する。呼び出し側は
    ``except AppError as exc`` で終了コードと通知メッセージを 1 箇所で決定する。
    """
    # HEALTH_EXPORT_DIR 事前チェック (存在 + ディレクトリ)
    if not config.health_export_dir.exists():
        raise HealthExportDirError(
            f"HEALTH_EXPORT_DIR が存在しません: {config.health_export_dir}",
            title="ihealth-relay: 設定不備 (HEALTH_EXPORT_DIR)",
            body=(
                f"指定ディレクトリ {config.health_export_dir} が存在しません。"
                ".env を確認してください。"
            ),
        )
    if not config.health_export_dir.is_dir():
        raise HealthExportDirError(
            f"HEALTH_EXPORT_DIR がディレクトリではありません: {config.health_export_dir}",
            title="ihealth-relay: 設定不備 (HEALTH_EXPORT_DIR)",
            body=(
                f"{config.health_export_dir} はディレクトリではありません。"
                ".env を確認してください。"
            ),
        )

    # fetch
    try:
        fetch_result = deps.fetcher(
            health_export_dir=config.health_export_dir,
            target_date=target_date,
            archive_root=config.archive_root,
            logger=logger,
        )
    except SourceError as exc:
        logger.error("データ取得に失敗しました: %s", exc)
        raise SourceAppError(
            str(exc),
            title=f"ihealth-relay: データ未到着 ({target_date})",
            body=(
                "iPhone 側で Health Auto Export の AutoSync を手動実行してください。\n"
                f"詳細: {exc}"
            ),
        ) from exc

    logger.info(
        "アーカイブ: %s (%d 成功 / %d 欠落)",
        fetch_result.archive_dir,
        len(fetch_result.archived),
        len(fetch_result.missing),
    )

    # parse (前日 sleep_analysis とマージ)
    max_hr = config.max_heart_rate(as_of=target_date)
    ctx = ParseContext(target_date=target_date, max_heart_rate=max_hr)
    sleep_overrides = deps.sleep_payload_builder(config, fetch_result, target_date, logger)
    health_data = deps.parser(
        fetch_result.archive_dir,
        target_date,
        ctx,
        payload_overrides=sleep_overrides,
    )

    populated = sum(
        1 for key, value in health_data.as_dict().items()
        if key != "date" and value is not None
    )
    logger.info("集計完了: target=%s populated=%d/13", target_date, populated)
    logger.debug("集計値:")
    for key, value in health_data.as_dict().items():
        logger.debug("  %s = %s", key, value)

    # publish (dry-run は DryRunPublisher で no-op)
    try:
        deps.publisher.publish(health_data)
    except MarkdownPublishError as exc:
        # Markdown publisher の I/O 失敗は AppError にラップして CLI に統一通知.
        # (Notion publisher 系は publisher 内部で NotionAppError を直接 raise する
        # ので別経路だが、Markdown publisher は Protocol 違反を避けるため例外を
        # 内部型 ``MarkdownPublishError`` に閉じ込めて、ここで AppError へ変換する.)
        logger.error("Markdown 出力に失敗しました: %s", exc)
        raise MarkdownAppError(
            str(exc),
            title=f"ihealth-relay: Markdown 出力失敗 ({target_date})",
            body=(
                f"対象日 {target_date} の Markdown 書き込みに失敗しました。\n"
                f"出力先ディレクトリの権限/容量を確認してください。\n"
                f"詳細: {exc}"
            ),
        ) from exc
    except StdoutPublishError as exc:
        logger.error("stdout 出力に失敗しました: %s", exc)
        raise StdoutAppError(
            str(exc),
            title=f"ihealth-relay: stdout 出力失敗 ({target_date})",
            body=(
                f"対象日 {target_date} の stdout 書き込みに失敗しました。\n"
                f"標準出力のリダイレクト先や DailyHealthData の値を確認してください。\n"
                f"詳細: {exc}"
            ),
        ) from exc
    except SQLitePublishError as exc:
        logger.error("SQLite 書き込みに失敗しました: %s", exc)
        raise SQLiteAppError(
            str(exc),
            title=f"ihealth-relay: SQLite 書き込み失敗 ({target_date})",
            body=(
                f"対象日 {target_date} の SQLite UPSERT に失敗しました。\n"
                f"DB ファイルの権限/容量/lock を確認してください。\n"
                f"詳細: {exc}"
            ),
        ) from exc
    except SlackPublishError as exc:
        logger.error("Slack 通知に失敗しました: %s", exc)
        raise SlackAppError(
            str(exc),
            title=f"ihealth-relay: Slack 通知失敗 ({target_date})",
            body=(
                f"対象日 {target_date} の Slack 通知に失敗しました。\n"
                f"Webhook URL の有効性とネットワークを確認してください。\n"
                f"詳細: {exc}"
            ),
        ) from exc
    return health_data


# ---------- 既定 deps factory + sleep merge ロジック (旧 __main__ から移植) ----------


def build_default_deps(
    config: Config,
    *,
    dry_run: bool,
    target_date: date,
    logger: logging.Logger,
    publisher_kind: str = "notion",
    markdown_output_dir_override: "Path | None" = None,
    sqlite_db_path_override: "Path | None" = None,
    slack_webhook_url_override: "str | None" = None,
) -> PipelineDeps:
    """本番用の既定 deps を組み立てる.

    Args:
        config: ``.env`` から読んだ設定.
        dry_run: ``True`` なら publisher 選択を ``"dryrun"`` で上書きし、HTTP を
            1 回も発射しない (= secret / token を実際に使わない). これは
            ``--publisher`` の指定より強い (= 安全側に倒す).
        target_date: 対象日.
        logger: 注入ロガー.
        publisher_kind: ``"notion"`` (既定) / ``"markdown"`` / ``"dryrun"``.
            ``dry_run=True`` なら無視される.
        markdown_output_dir_override: ``--markdown-out`` CLI 引数で渡された
            出力先パス. ``None`` なら ``config.markdown_output_dir`` (= ``.env``
            の ``MARKDOWN_OUTPUT_DIR``) を使う. どちらも ``None`` で
            ``publisher_kind="markdown"`` のときは :class:`ConfigAppError`.

    ``config.toml`` がプロジェクトルートに存在すれば
    ``[publishers.notion.fields.<field>].property`` の override を読み出して
    Notion publisher に渡す。不在なら :data:`PROPERTY_MAP` の既定値が使われる
    (= 既存運用は無修正で継続).
    """
    if publisher_kind not in PUBLISHER_KINDS:
        raise ConfigAppError(
            f"未知の publisher_kind: {publisher_kind!r} "
            f"(許可値: {PUBLISHER_KINDS})",
            title="ihealth-relay: 未知の publisher 指定",
            body=(
                f"--publisher で指定された値 {publisher_kind!r} は未対応です。\n"
                f"許可値: {', '.join(PUBLISHER_KINDS)}"
            ),
        )

    # dry_run フラグは publisher 選択より強い (安全側で no-op にする).
    effective_kind = "dryrun" if dry_run else publisher_kind

    # config.toml の Notion property override 読み込み (Phase 1 B1 / Issue #18).
    # Markdown publisher は現状 override 未対応だが、"config.toml が壊れているなら
    # どの publisher でも fail-fast すべき" 原則は保つので呼び出しは publisher
    # 種別に関わらず行う.
    property_overrides, date_property_override = _load_notion_overrides(logger)

    publisher: Publisher
    if effective_kind == "dryrun":
        publisher = DryRunPublisher(
            logger=logger, property_overrides=property_overrides,
        )
    elif effective_kind == "markdown":
        output_dir = markdown_output_dir_override or config.markdown_output_dir
        if output_dir is None:
            raise ConfigAppError(
                "Markdown publisher の出力先が未設定です",
                title="ihealth-relay: Markdown 出力先未設定",
                body=(
                    "--publisher markdown を指定するには、出力先ディレクトリを\n"
                    "次のいずれかで指定してください:\n"
                    "  - .env に MARKDOWN_OUTPUT_DIR=<絶対パス>\n"
                    "  - CLI 引数 --markdown-out <絶対パス>"
                ),
            )
        publisher = MarkdownPublisher(
            output_dir=output_dir,
            target_date=target_date,
            logger=logger,
        )
    elif effective_kind == "stdout":
        # stdout publisher は出力先設定不要 (sys.stdout に書く).
        publisher = StdoutPublisher(
            target_date=target_date, logger=logger,
        )
    elif effective_kind == "sqlite":
        db_path = sqlite_db_path_override or config.sqlite_db_path
        if db_path is None:
            raise ConfigAppError(
                "SQLite publisher の DB パスが未設定です",
                title="ihealth-relay: SQLite DB パス未設定",
                body=(
                    "--publisher sqlite を指定するには、DB ファイルパスを\n"
                    "次のいずれかで指定してください:\n"
                    "  - .env に SQLITE_DB_PATH=<絶対パス>\n"
                    "  - CLI 引数 --sqlite-path <絶対パス>"
                ),
            )
        publisher = SQLitePublisher(
            db_path=db_path, target_date=target_date, logger=logger,
        )
    elif effective_kind == "slack":
        webhook = slack_webhook_url_override or config.slack_webhook_url
        if not webhook:
            raise ConfigAppError(
                "Slack publisher の Webhook URL が未設定です",
                title="ihealth-relay: Slack Webhook 未設定",
                body=(
                    "--publisher slack を指定するには、Incoming Webhook URL を\n"
                    "次のいずれかで指定してください:\n"
                    "  - .env に SLACK_WEBHOOK_URL=<URL>\n"
                    "  - CLI 引数 --slack-webhook <URL>"
                ),
            )
        publisher = SlackPublisher(
            webhook_url=webhook, target_date=target_date, logger=logger,
        )
    elif effective_kind == "notion":
        # が None でも (= require_notion=False で .env を読んだ場合) Notion
        # publisher 経路に来た時点で ConfigAppError. これは "markdown 用の
        # .env で誤って notion publisher を起動した" ような操作ミス検知.
        if not config.notion_secret or not config.database_id:
            missing = [
                name for name, value in (
                    ("NOTION_SECRET", config.notion_secret),
                    ("DATABASE_ID", config.database_id),
                ) if not value
            ]
            raise ConfigAppError(
                f"Notion publisher に必要な .env キーが未設定: {', '.join(missing)}",
                title="ihealth-relay: Notion 設定不備",
                body=(
                    f".env に {', '.join(missing)} を設定するか、\n"
                    "--publisher markdown / --dry-run を指定してください。"
                ),
            )
        client = NotionClient(
            token=config.notion_secret,
            database_id=config.database_id,
            logger=logger,
            date_property_name=date_property_override,
        )
        publisher = NotionPublisher(
            client=client, target_date=target_date, logger=logger,
            property_overrides=property_overrides,
        )
    else:  # pragma: no cover — PUBLISHER_KINDS 検証で到達不能
        raise ConfigAppError(f"内部矛盾: effective_kind={effective_kind!r}")

    return PipelineDeps(
        fetcher=fetch_all_metrics,
        parser=parse_all,
        publisher=publisher,
        sleep_payload_builder=_build_merged_sleep_payload,
    )


def _load_notion_property_overrides(
    logger: logging.Logger,
) -> "dict[str, str]":
    """旧 API: 後方互換 alias. 新コードは :func:`_load_notion_overrides` を使う."""
    overrides, _date = _load_notion_overrides(logger)
    return overrides


def _load_notion_overrides(
    logger: logging.Logger,
) -> "tuple[dict[str, str], str | None]":
    """``config.toml`` から Notion プロパティ名 + date_property override を読み込む.

    fail-soft の対象は **ファイル不在のみ**:
        - ``config.toml`` 不在 → 空 dict + None (= override なしで既定値を使う)
        - ``config.toml`` 存在 + パース失敗 / 構造不正 → ConfigAppError raise
        - override 後の Notion プロパティ名が衝突 → ConfigAppError raise

    "存在するファイルは設定する意思" と "不在は設定しない意思" を区別する.
    silent data loss を防ぐため、存在するなら fail-fast.

    Returns:
        ``(property_overrides, date_property_override)`` のタプル.
        date_property_override が ``None`` のとき
        :attr:`NotionClient.DEFAULT_DATE_PROPERTY_NAME` が使われる.
    """
    from ihealth import config_file
    from ihealth._toml import TomlParseError
    from ihealth.config_file import ConfigFileError
    from ihealth.publishers.notion import PROPERTY_MAP

    project_root = Path(__file__).resolve().parent.parent.parent
    path = config_file.find_config_path(project_root)
    if path is None:
        return {}, None

    valid_fields = frozenset(PROPERTY_MAP.keys())
    try:
        data = config_file.load_config_toml(path)
        overrides = config_file.extract_notion_property_overrides(
            data, valid_field_names=valid_fields,
        )
        date_override = config_file.extract_notion_date_property(data)
    except (TomlParseError, ConfigFileError) as exc:
        raise ConfigAppError(
            f"config.toml の構造エラー: {exc}",
            title="ihealth-relay: config.toml が不正です",
            body=(
                f"config.toml ({path}) を確認してください。\n"
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc
    except OSError as exc:
        raise ConfigAppError(
            f"config.toml の読み込みに失敗: {exc}",
            title="ihealth-relay: config.toml にアクセスできません",
            body=(
                f"config.toml ({path}) を確認してください "
                "(権限 / 文字エンコーディング)。\n"
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc

    _ensure_no_property_collisions(overrides, logger)
    return overrides, date_override


def _ensure_no_property_collisions(
    overrides: "dict[str, str]",
    logger: logging.Logger,
) -> None:
    """override 後の Notion プロパティ名が衝突していないことを保証する.

    複数の :class:`DailyHealthData` フィールドが同じ Notion プロパティ名にマップ
    されると、:func:`build_properties` の dict 構築で後勝ち上書きが起き、
    歩数や睡眠時間が silent に消える危険がある.

    Raises:
        ConfigAppError: 衝突を検知した場合.
    """
    from ihealth.publishers.notion import PROPERTY_MAP

    seen: "dict[str, str]" = {}
    for field_name, (default_prop_name, _converter) in PROPERTY_MAP.items():
        prop_name = overrides.get(field_name, default_prop_name)
        prev_field = seen.get(prop_name)
        if prev_field is not None and prev_field != field_name:
            raise ConfigAppError(
                f"Notion プロパティ名 {prop_name!r} が複数フィールドで衝突: "
                f"{prev_field} と {field_name}",
                title="ihealth-relay: config.toml の property 衝突",
                body=(
                    f"config.toml で {prev_field} と {field_name} が "
                    f"両方とも {prop_name!r} にマップされています。\n"
                    "Notion API への送信で後勝ち上書きが起き、データが消えます。\n"
                    "config.toml を編集して衝突を解消してください。"
                ),
            )
        seen[prop_name] = field_name


def _load_sleep_json_archive_first(
    *,
    target_date: date,
    archive_root: Path,
    config: Config,
    allow_live_fetch: bool,
    logger: logging.Logger,
) -> "list[dict]":
    """指定日の ``sleep_analysis.json`` を archive-first で読み込み data 配列を返す。

    Issue #14 の挙動を workflow に移植。手順は同じ:
    1. archive_root/target_date/sleep_analysis.json を先に読む
    2. 無ければ allow_live_fetch=True のときだけ live 解凍
    3. 取れなければ空リスト
    """
    archive_path = archive_root / target_date.isoformat() / f"{_SLEEP_ANALYSIS_METRIC_DIR}.json"
    if not archive_path.exists() and allow_live_fetch:
        fetched = fetch_metric_for_date(
            health_export_dir=config.health_export_dir,
            metric_dir_name=_SLEEP_ANALYSIS_METRIC_DIR,
            target_date=target_date,
            archive_root=archive_root,
            logger=logger,
        )
        if fetched is not None:
            archive_path = fetched
    if not archive_path.exists():
        return []
    try:
        payload = json.loads(archive_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("sleep_analysis の読み込みに失敗 (%s): %s", target_date, exc)
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return data


def _build_merged_sleep_payload(
    config: Config,
    fetch_result: FetchResult,
    target_date: date,
    logger: logging.Logger,
) -> "dict[str, dict]":
    """当日 + 前日の sleep_analysis.json を結合した payload を parse_all 向けに組む。

    Issue #14 の仕様を workflow に移植。archive-first で当日と前日を読んで結合。
    """
    today_data = _load_sleep_json_archive_first(
        target_date=target_date,
        archive_root=config.archive_root,
        config=config,
        allow_live_fetch=False,
        logger=logger,
    )
    prev_date = target_date - timedelta(days=1)
    prev_data = _load_sleep_json_archive_first(
        target_date=prev_date,
        archive_root=config.archive_root,
        config=config,
        allow_live_fetch=True,
        logger=logger,
    )
    if not today_data and not prev_data:
        return {}
    if prev_data:
        logger.debug(
            "前日 (%s) の sleep_analysis を結合: +%d レコード",
            prev_date, len(prev_data),
        )
    return {
        _SLEEP_ANALYSIS_METRIC_DIR: {
            "metric": "Sleep Analysis (merged)",
            "data": [*today_data, *prev_data],
        }
    }
