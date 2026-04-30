"""エントリポイント: ``python3 -m ihealth``

起動確認 → 設定読込 → AutoSync 配下の ``.hae`` 解凍 →
``data/health/YYYY-MM-DD/{metric}.json`` にアーカイブ → 14 フィールド集計 →
``--publisher`` で選択した sink (notion / markdown / stdout / sqlite / slack /
dryrun) に転記 → 失敗時 Reminders.app 通知までを実行する。

使い方::

    PYTHONPATH=src /usr/bin/python3 -m ihealth                      # 昨日分
    PYTHONPATH=src /usr/bin/python3 -m ihealth --date 2026-04-22    # 指定日
    PYTHONPATH=src /usr/bin/python3 -m ihealth --dry-run --verbose  # 検証用

終了コード::

    0: 正常終了
    2: ``--date`` / ``--markdown-out`` / ``--sqlite-path`` / ``--slack-webhook`` の形式エラー
    3: ``.env`` の設定不備 (ConfigAppError)
    4: ``HEALTH_EXPORT_DIR`` 非存在 (HealthExportDirError)
    5: ``.hae`` 取得失敗 (SourceAppError)
    6: Notion API エラー / 日記ページ未作成 (NotionAppError)
    7: Markdown 出力失敗 (MarkdownAppError, Phase 1 A3-md)
    8: Stdout 出力失敗 (StdoutAppError, Phase 1 A3-stdout)
    9: SQLite 書き込み失敗 (SQLiteAppError, Phase 1 A3-sqlite)
    10: Slack 通知失敗 (SlackAppError, Phase 1 A3-slack)

終了コード 3 以上の失敗はすべて Reminders.app にリマインダーを追加する
(``--dry-run`` 指定時も通知ルートは同じ、ただし通常は dry-run でエラーは起こらない)。

**Issue #16 リファクタ後**: 本モジュールは **引数処理 + 設定読込 +
AppError → 終了コード + Reminders 通知 のマッピング** だけに責務を絞る。
本体パイプラインは :mod:`ihealth.workflow` に分離してテスト容易性を確保した。
"""

from __future__ import annotations

import sys

_MIN_PYTHON = (3, 9)

if sys.version_info < _MIN_PYTHON:
    sys.stderr.write(
        f"Python {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}+ が必要です "
        f"(現在: {sys.version_info.major}.{sys.version_info.minor})\n"
    )
    sys.exit(2)

import argparse
from datetime import date, timedelta
from pathlib import Path

from ihealth.config import ConfigError, load_config
from ihealth.errors import AppError, ConfigAppError
from ihealth.logger import configure as configure_logger
from ihealth.notifier import notify_failure
from ihealth.workflow import PUBLISHER_KINDS, build_default_deps, run_pipeline


def _parse_args(argv: "list[str] | None" = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ihealth",
        description=(
            "Apple Health データを集計し選択した publisher (Notion / Markdown 等) "
            "に転記する"
        ),
    )
    parser.add_argument(
        "--date", metavar="YYYY-MM-DD", help="対象日付 (未指定なら昨日)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Notion に書き込まず、取得・変換結果をログに出すだけ",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="DEBUG レベルのログを出力",
    )
    parser.add_argument(
        "--publisher",
        choices=PUBLISHER_KINDS,
        default="notion",
        help=(
            "出力先 publisher を選択 (既定: notion). "
            "--dry-run が指定されたら本選択は無視される."
        ),
    )
    parser.add_argument(
        "--markdown-out",
        metavar="PATH",
        default=None,
        help=(
            "--publisher markdown のときに使う出力ディレクトリ. "
            "未指定なら .env の MARKDOWN_OUTPUT_DIR を使う."
        ),
    )
    parser.add_argument(
        "--sqlite-path",
        metavar="PATH",
        default=None,
        help=(
            "--publisher sqlite のときに使う DB ファイルパス. "
            "未指定なら .env の SQLITE_DB_PATH を使う."
        ),
    )
    parser.add_argument(
        "--slack-webhook",
        metavar="URL",
        default=None,
        help=(
            "--publisher slack のときに使う Incoming Webhook URL. "
            "未指定なら .env の SLACK_WEBHOOK_URL を使う."
        ),
    )
    return parser.parse_args(argv)


def _resolve_slack_webhook(raw: "str | None") -> "str | None":
    """``--slack-webhook`` の値を ``str`` に変換. 未指定なら ``None``.

    検証は :func:`ihealth.publishers.slack.validate_webhook_url` に委譲して
    config 側と同一基準にする (https / host allow-list / userinfo 禁止 /
    /services/ path / query 禁止). secret token を含むためエラーメッセージ
    に URL 本文は出さない (validator が source_label しか出さない).
    """
    if raw is None:
        return None
    if not raw.strip():
        raise ValueError(
            "--slack-webhook は空文字を指定できません (env 展開ミスの可能性). "
            "オプションを外すか、URL を明示してください"
        )
    from ihealth.publishers.slack import validate_webhook_url
    return validate_webhook_url(raw.strip(), source_label="--slack-webhook")


def _resolve_markdown_out(raw: "str | None") -> "Path | None":
    """``--markdown-out`` の値を ``Path`` (絶対パス) に変換. 未指定なら ``None``.

    既存パスがファイル (= ディレクトリ
    ではない) のときは ``ValueError`` で reject. ``--sqlite-path`` の "ファイル
    限定" と対称な「ディレクトリ限定」契約を CLI / .env の両側で揃える.
    """
    path = _resolve_cli_path(raw, flag_name="--markdown-out")
    if path is not None and path.exists() and not path.is_dir():
        raise ValueError(
            f"--markdown-out はディレクトリの絶対パスを指定してください "
            f"(現在ファイル): {path}"
        )
    return path


def _resolve_sqlite_path(raw: "str | None") -> "Path | None":
    """``--sqlite-path`` の値を ``Path`` (絶対パス) に変換. 未指定なら ``None``.

    既存ディレクトリは ``ValueError``
    で reject. ``Config._parse_optional_file_path`` と CLI 側で fail-fast
    意味論を揃え、SQLite publisher 内部での遅延エラーを防ぐ.
    """
    path = _resolve_cli_path(raw, flag_name="--sqlite-path")
    if path is not None and path.exists() and path.is_dir():
        raise ValueError(
            f"--sqlite-path はファイルの絶対パスを指定してください "
            f"(現在ディレクトリ): {path}"
        )
    return path


def _resolve_cli_path(raw: "str | None", *, flag_name: str) -> "Path | None":
    """``~/`` のみ展開し、絶対パス検証して ``Path`` を返す共通ヘルパー.

    ``Config._parse_optional_path`` と同じ規則を CLI 引数にも適用. 不正値は
    ``ValueError`` で stderr 経由 exit 2 (argparse 標準と同等).

    空文字は ``None`` ではなく
    ``ValueError`` で reject. ``--sqlite-path "$SQLITE_DB_PATH"`` の env 展開
    ミスで空文字が来た場合に ``.env`` 既定値へ silent fallback して誤った
    DB に書く事故を防ぐ (= fail-fast).
    """
    if raw is None:
        return None
    if not raw.strip():
        raise ValueError(
            f"{flag_name} は空文字を指定できません (env 展開ミスの可能性). "
            "オプションを外すか、絶対パスを明示してください"
        )
    raw = raw.strip()
    if raw.startswith("~/"):
        path = Path.home() / raw[2:]
    elif raw.startswith("~"):
        raise ValueError(
            f"{flag_name} は ~/... または絶対パスで指定してください "
            "(~user/... のような user 指定は未対応)"
        )
    else:
        path = Path(raw)
    if not path.is_absolute():
        raise ValueError(f"{flag_name} は絶対パスで指定してください")
    return path


def _resolve_target_date(date_arg: "str | None") -> date:
    """``--date`` 引数を ``datetime.date`` に変換。未指定なら昨日。"""
    if date_arg is None:
        return date.today() - timedelta(days=1)
    try:
        return date.fromisoformat(date_arg)
    except ValueError as exc:
        raise ValueError(
            f"--date の形式が不正です (YYYY-MM-DD): {date_arg!r}"
        ) from exc


def main(argv: "list[str] | None" = None) -> int:
    """エントリポイント本体。終了コードを返す。

    例外フロー:
    - argparse 不正 → argparse が exit 2
    - --date 不正 → ValueError → sys.stderr + return 2
    - ConfigError → ConfigAppError に変換 → AppError catch 経路
    - HealthExportDirError / SourceAppError / NotionAppError → AppError catch 経路
    - 想定外の例外 → traceback + return 1 (catch-all)
    """
    args = _parse_args(argv)

    # publisher 選択に応じて NOTION_SECRET / DATABASE_ID 必須を切り替える.
    # --publisher notion (or --dry-run 経由で notion 経路から ``DryRunPublisher``
    # に切り替わるケース) でだけ Notion 系の値を要求する. dry-run / markdown では
    # Notion なしで .env が組めるべき.
    # 注意: --dry-run のとき build_default_deps は NotionClient を生成しないので
    # 実 secret が欠けていても問題ない. よって require_notion=False でよい.
    effective_kind = "dryrun" if args.dry_run else args.publisher
    require_notion = effective_kind == "notion"

    # 設定読込を最初に: ConfigError 時は logger が未初期化なので stderr に書きつつ通知。
    # OSError / PermissionError / UnicodeDecodeError も同じ扱い (.env アクセス失敗 =
    # 設定系の早期エラー) にマップして exit 3 + 通知。
    try:
        config = load_config(require_notion=require_notion)
    except ConfigError as exc:
        sys.stderr.write(f"設定エラー: {exc}\n")
        notify_failure(
            "ihealth-relay: 設定不備",
            f".env の読み込みに失敗: {exc}",
        )
        return ConfigAppError.exit_code
    except (OSError, UnicodeDecodeError) as exc:
        # .env 読めない (permission / encoding 不正 / デバイス I/O エラー) も
        # 設定不備として扱う。logger 未初期化なので stderr + Reminders のみ。
        sys.stderr.write(f"設定エラー (.env アクセス失敗): {exc}\n")
        notify_failure(
            "ihealth-relay: 設定不備 (.env アクセス失敗)",
            f".env の読み込みに失敗: {type(exc).__name__}: {exc}",
        )
        return ConfigAppError.exit_code

    log_level = "debug" if args.verbose else config.log_level
    logger = configure_logger(log_level)

    try:
        target_date = _resolve_target_date(args.date)
    except ValueError as exc:
        sys.stderr.write(f"引数エラー: {exc}\n")
        return 2

    try:
        markdown_out_override = _resolve_markdown_out(args.markdown_out)
        sqlite_path_override = _resolve_sqlite_path(args.sqlite_path)
        slack_webhook_override = _resolve_slack_webhook(args.slack_webhook)
    except ValueError as exc:
        sys.stderr.write(f"引数エラー: {exc}\n")
        return 2

    logger.info(
        "ihealth-relay 起動 (target=%s, dry_run=%s, publisher=%s)",
        target_date, args.dry_run, args.publisher,
    )
    masked_db = f"...{config.database_id[-8:]}" if config.database_id else "(unset)"
    logger.debug(
        "設定: database_id=%s, health_export_dir=%s",
        masked_db, config.health_export_dir,
    )

    # build_default_deps は config.toml を読み込んで Publisher を組み立てる際に
    # ConfigAppError を raise しうる.
    # 既知エラー経路として exit code + Reminders 通知に乗せるため try/except 内で呼ぶ.
    try:
        deps = build_default_deps(
            config,
            dry_run=args.dry_run,
            target_date=target_date,
            logger=logger,
            publisher_kind=args.publisher,
            markdown_output_dir_override=markdown_out_override,
            sqlite_db_path_override=sqlite_path_override,
            slack_webhook_url_override=slack_webhook_override,
        )
        run_pipeline(config, target_date, deps, logger)
    except AppError as exc:
        logger.error("%s", exc.user_message)
        notify_failure(exc.title, exc.body)
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - top-level safety net
        # 想定外の例外 (parser 内の assertion 失敗、Publisher の素例外など) も
        # silent に traceback で終わらせず通知する。exit code 1 で運用者に
        # 「既知エラー (3-6) ではない何か」が起きたと区別させる。
        logger.exception("想定外エラーでパイプライン失敗")
        notify_failure(
            "ihealth-relay: 想定外エラー",
            f"logs/run.log を確認してください。\n{type(exc).__name__}: {exc}",
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
