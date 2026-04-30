"""アプリ固有エラーの統一型。

設計意図 (Issue #16): ``__main__.main()`` で全失敗経路を統一ハンドリングするため、
各モジュールの技術的エラー (ConfigError / SourceError / NotionError) を
**ユーザー向けの意味論** を持つ ``AppError`` サブクラスに包んで CLI 層に届ける。

- ``exit_code``: プロセスの終了コード (launchd ログで失敗種別を区別するため)
- ``user_message``: Reminders 通知のタイトル/本文に使う日本語メッセージ
- ``category``: 通知カテゴリ (将来 Slack 等への経路分岐で使う余地)

CLI 層 (``__main__.main``) は ``AppError`` を catch して 1 箇所で
``notify_failure`` + ``return exit_code`` を実行する。これにより各エラー経路で
notify_failure を書き散らす必要がなくなる。
"""

from __future__ import annotations


class AppError(Exception):
    """ユーザー操作で発生する既知エラー。CLI 終了コードと通知メッセージを持つ。

    ``exit_code`` はサブクラスで class attribute として定義する。
    ``user_message`` は ``__init__`` で受け取り、そのまま ``str(exc)`` として使える。
    """

    #: サブクラスごとに 0 以外の値で override する
    exit_code: int = 1

    def __init__(
        self,
        user_message: str,
        *,
        title: "str | None" = None,
        body: "str | None" = None,
        exit_code: "int | None" = None,
    ) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.title = title or user_message
        self.body = body or user_message
        if exit_code is not None:
            self.exit_code = exit_code


class ConfigAppError(AppError):
    """``.env`` の設定不備。``exit_code=3``。"""

    exit_code = 3


class HealthExportDirError(AppError):
    """``HEALTH_EXPORT_DIR`` 非存在 / 非ディレクトリ。``exit_code=4``。"""

    exit_code = 4


class SourceAppError(AppError):
    """``.hae`` 取得 / 解凍に失敗。``exit_code=5``。"""

    exit_code = 5


class NotionAppError(AppError):
    """Notion API 呼び出し失敗 / 日記ページ未作成。``exit_code=6``。"""

    exit_code = 6


class MarkdownAppError(AppError):
    """Markdown publisher の書き込み失敗 / 出力ディレクトリ不正。``exit_code=7``。

    OSS publisher 群 (Phase 1 A3-md) で導入。``output_dir`` が壊れている、
    ディスクフルで write できない等の I/O 系失敗を Notion 系と区別する。
    "出力先未指定" は本エラーではなく :class:`ConfigAppError` (= ``.env`` 不備系)
    として扱う。
    """

    exit_code = 7


class StdoutAppError(AppError):
    """Stdout publisher の書き込み / 値検証失敗。``exit_code=8``。

    OSS publisher 群 (Phase 1 A3-stdout) で導入。stdout がリダイレクト先で
    write 失敗 (disk full 等) した場合や、``DailyHealthData`` の値が型違反の
    場合に raise される。``BrokenPipeError`` (pipe 受信側が閉じた) は publisher
    内で silent に握り潰すので、本クラスは raise されない。
    """

    exit_code = 8


class SQLiteAppError(AppError):
    """SQLite publisher の書き込み / 値検証失敗。``exit_code=9``。

    OSS publisher 群 (Phase 1 A3-sqlite) で導入。DB ファイル作成失敗 / SQL
    エラー / disk full / lock 競合 / 値検証失敗 に raise される。"DB パス
    未指定" は本エラーではなく :class:`ConfigAppError` (= ``.env`` 不備系)
    として扱う。
    """

    exit_code = 9


class SlackAppError(AppError):
    """Slack publisher の Webhook 失敗 / 値検証失敗。``exit_code=10``。

    OSS publisher 群 (Phase 1 A3-slack) で導入。Webhook URL への HTTPS POST
    失敗 / Slack 応答異常 / 値検証失敗に raise される。"Webhook URL 未指定"
    は本エラーではなく :class:`ConfigAppError` (= ``.env`` 不備系) として扱う。
    """

    exit_code = 10
