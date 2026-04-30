"""`.env` の読み込みと実行時設定の保持。

外部依存を避けるため `python-dotenv` は使わず、必要最低限のパーサを自前で実装する。

対応記法:
- `KEY=value`
- `KEY="value with spaces"` / `KEY='value'` (両端のクオートは剥がす)
- `#` で始まる行はコメント
- 空行はスキップ
- `~/` で始まるパスはホームディレクトリに展開 (中間の `~` は文字どおり)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ihealth.logger import ALLOWED_LEVELS as _ALLOWED_LEVELS


class ConfigError(RuntimeError):
    """`.env` の読み込み・検証で発生する例外。"""


@dataclass(frozen=True)
class Config:
    """実行時設定。必須項目は `load_config()` で検証済み。

    Phase 1 A3-md で ``markdown_output_dir`` を optional で追加。``--publisher
    markdown`` を指定したときに使う書き出し先で、未指定なら ``None``。Notion
    publisher を使う既定運用には影響しない (= 既存 .env を編集せずに動く)。
    """

    notion_secret: "str | None"
    database_id: "str | None"
    health_export_dir: Path
    archive_root: Path
    birth_date: date
    log_level: str = "info"
    markdown_output_dir: "Path | None" = None
    sqlite_db_path: "Path | None" = None
    slack_webhook_url: "str | None" = None

    def age_years(self, as_of: "date | None" = None) -> int:
        """``as_of`` 時点の満年齢 (整数)。``None`` なら今日基準。

        2/29 生まれの扱い: 平年は 3/1 に歳を取る (``3/1 >= 2/29`` と比較されるため)。
        2/28 を記念日にしたいユーザーは仕様変更が必要。
        """
        today = as_of or date.today()
        years = today.year - self.birth_date.year
        if (today.month, today.day) < (self.birth_date.month, self.birth_date.day):
            years -= 1
        return years

    def max_heart_rate(self, as_of: "date | None" = None) -> float:
        """最大心拍数 (Tanaka 式: ``205.8 − 0.685 × age``)。

        Google Fit の Heart Points 算出で使われている公式式に揃える
        (https://support.google.com/fit/answer/7619539)。
        旧来の ``220 − age`` より若年側で低め、壮年側でも精度が高い。
        """
        age = self.age_years(as_of=as_of)
        return 205.8 - (0.685 * age)


def load_config(
    env_path: Path | None = None, *, require_notion: bool = True,
) -> Config:
    """プロジェクトルートの `.env` を読み込んで `Config` を返す。

    Args:
        env_path: 明示的な `.env` パス。`None` の場合はプロジェクトルートを使用。
        require_notion: ``NOTION_SECRET`` / ``DATABASE_ID`` を必須として扱うか.
            既定 ``True`` で **既存の launchd Notion 運用を壊さない**.
            ``--publisher markdown`` (Notion 不使用) のときは ``False`` を渡し、
            両キーが欠落していても ``Config.notion_secret`` / ``database_id``
            が ``None`` の状態で組み立てる.

    Raises:
        ConfigError: `.env` が存在しない、または必須キーが欠落している場合。
            ``require_notion=False`` でも ``HEALTH_EXPORT_DIR`` / ``BIRTH_DATE``
            は常に必須.
    """
    resolved_path = env_path if env_path is not None else _project_root() / ".env"

    if not resolved_path.exists():
        raise ConfigError(f".env が見つかりません: {resolved_path}")

    values = _parse_env_file(resolved_path)

    required = ["HEALTH_EXPORT_DIR", "BIRTH_DATE"]
    if require_notion:
        required = ["NOTION_SECRET", "DATABASE_ID", *required]
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ConfigError(
            f".env に必須キーが不足しています: {', '.join(missing)}"
        )

    try:
        birth_date = date.fromisoformat(values["BIRTH_DATE"])
    except ValueError as exc:
        # 不正値は個人情報 (実誕生日の可能性) なので、値そのものはログに出さない。
        raise ConfigError(
            "BIRTH_DATE が ISO8601 (YYYY-MM-DD) 形式ではありません"
        ) from exc

    health_export_dir = _parse_health_export_dir(values["HEALTH_EXPORT_DIR"])

    # Issue #17: LOG_LEVEL の許可値を明示検証 (typo で静かに INFO に落ちるのを防ぐ)
    log_level = values.get("LOG_LEVEL", "info").strip().lower()
    if log_level not in _ALLOWED_LEVELS:
        raise ConfigError(
            f"LOG_LEVEL が不正です (許可値: {sorted(_ALLOWED_LEVELS)}): "
            f"{log_level!r}"
        )

    # Phase 1 A3-md: optional の Markdown 出力先。未指定なら None で持たせる
    # (Notion publisher を使う既定運用は無修正で動く).
    markdown_output_dir_raw = values.get("MARKDOWN_OUTPUT_DIR", "").strip()
    markdown_output_dir: "Path | None"
    if markdown_output_dir_raw:
        markdown_output_dir = _parse_optional_dir_path(
            markdown_output_dir_raw, key_name="MARKDOWN_OUTPUT_DIR",
        )
    else:
        markdown_output_dir = None

    # Phase 1 A3-sqlite: optional の SQLite DB ファイルパス. 未指定なら None.
    # ディレクトリではなく **ファイル** パスを受ける (例: ~/health.db).
    sqlite_db_path_raw = values.get("SQLITE_DB_PATH", "").strip()
    sqlite_db_path: "Path | None"
    if sqlite_db_path_raw:
        sqlite_db_path = _parse_optional_file_path(
            sqlite_db_path_raw, key_name="SQLITE_DB_PATH",
        )
    else:
        sqlite_db_path = None

    # Phase 1 A3-slack: optional の Slack Incoming Webhook URL.
    # secret token を URL に含むので Path 型ではなく str のまま保持する.
    slack_webhook_raw = values.get("SLACK_WEBHOOK_URL", "").strip()
    slack_webhook_url = _validate_slack_webhook_url(
        slack_webhook_raw, key_name="SLACK_WEBHOOK_URL",
    ) if slack_webhook_raw else None

    # NOTION_SECRET / DATABASE_ID は require_notion=False のとき値が無くても
    # OK. その場合は ``None`` を保持し、Notion 経路を試みた時点で
    # build_default_deps が ConfigAppError を raise する設計.
    notion_secret = values.get("NOTION_SECRET") or None
    database_id = values.get("DATABASE_ID") or None

    return Config(
        notion_secret=notion_secret,
        database_id=database_id,
        health_export_dir=health_export_dir,
        archive_root=_project_root() / "data" / "health",
        birth_date=birth_date,
        log_level=log_level,
        markdown_output_dir=markdown_output_dir,
        sqlite_db_path=sqlite_db_path,
        slack_webhook_url=slack_webhook_url,
    )


def _validate_slack_webhook_url(raw: str, *, key_name: str) -> str:
    """Slack Incoming Webhook URL の構造を検証する.

    実体は :func:`ihealth.publishers.slack.validate_webhook_url` に委譲.
    検証内容:
    - ``https://`` 必須 (HTTP / file:// は reject)
    - host は ``hooks.slack.com`` / ``hooks.slack-gov.com`` の allow-list
    - userinfo (``https://user:pass@host/...``) 禁止
    - path は ``/services/`` で始まる必要
    - query / fragment 禁止

    "実際に届くか" は検証しない (= 起動時の通信を発生させない).
    Slack Webhook の secret token は URL に含まれるので、エラーメッセージに
    URL 全体は出さない (validator が source_label しか露出しない).
    """
    from ihealth.publishers.slack import validate_webhook_url
    try:
        return validate_webhook_url(raw, source_label=key_name)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None


def _parse_optional_dir_path(raw: str, *, key_name: str) -> Path:
    """``HEALTH_EXPORT_DIR`` と同じ規則で ``~/`` のみ展開し絶対パス検証する.

    既存パスが **ファイル** (= ディレクトリではない) だった場合は
    :class:`ConfigError` で fail-fast. ``_parse_optional_file_path`` の対称契約として、CLI / .env
    両側で「ディレクトリ限定」を揃える. 存在しないパス (= 初回起動で publisher
    が mkdir で作るケース) は許可.
    """
    path = _parse_optional_path(raw, key_name=key_name)
    if path.exists() and not path.is_dir():
        raise ConfigError(
            f"{key_name} はディレクトリの絶対パスを指定してください "
            f"(現在ファイル): {path}"
        )
    return path


def _parse_optional_file_path(raw: str, *, key_name: str) -> Path:
    """ファイルパス受け取り用 (= 既存ディレクトリは拒否).

    ``SQLITE_DB_PATH`` のように「ファイル単体を絶対パスで指定」させたいキーで使う.
    既存パスがディレクトリだった場合 (例: ``SQLITE_DB_PATH=/tmp/`` のような
    typo) は :class:`ConfigError` で fail-fast. 既存ディレクトリを通すと、後段の SQLite publisher が
    ``connect()`` で OSError になり ConfigAppError(3) ではなく
    SQLiteAppError(9) に遅延してしまう.

    存在しないパス (= 初回起動で DB を新規作成するケース) は許可する.
    """
    path = _parse_optional_path(raw, key_name=key_name)
    if path.exists() and path.is_dir():
        raise ConfigError(
            f"{key_name} はファイルの絶対パスを指定してください "
            f"(現在ディレクトリ): {path}"
        )
    return path


def _parse_optional_path(raw: str, *, key_name: str) -> Path:
    if raw.startswith("~/"):
        path = Path.home() / raw[2:]
    elif raw.startswith("~"):
        raise ConfigError(
            f"{key_name} は ~/... または絶対パスで指定してください "
            "(~user/... のような user 指定は未対応)"
        )
    else:
        path = Path(raw)

    if not path.is_absolute():
        raise ConfigError(f"{key_name} は絶対パスで指定してください")
    return path


def _parse_health_export_dir(raw: str) -> Path:
    """``HEALTH_EXPORT_DIR`` を安全に Path に変換する。

    契約:
    - 先頭 ``~/`` のみ ホームディレクトリ展開 (``Path.expanduser`` の広範な挙動を避け、
      ``~root/...`` や ``~unknownuser/...`` を誤って解釈しない)
    - それ以外は絶対パス必須。相対パスはエラー

    中間の ``~`` (iCloud Drive の ``iCloud~com~ifunography~HealthExport`` など) は
    フォルダ名の一部として文字通り扱う。
    """
    if raw.startswith("~/"):
        path = Path.home() / raw[2:]
    elif raw.startswith("~"):
        raise ConfigError(
            "HEALTH_EXPORT_DIR は ~/... または絶対パスで指定してください "
            "(~user/... のような user 指定は未対応)"
        )
    else:
        path = Path(raw)

    if not path.is_absolute():
        raise ConfigError("HEALTH_EXPORT_DIR は絶対パスで指定してください")
    return path


def _parse_env_file(path: Path) -> "dict[str, str]":
    """`.env` ファイル全体をパースして dict を返す。

    ``utf-8-sig`` で読み込むことで BOM 付き UTF-8 (Windows 系 GUI エディタや
    いくつかのツールが付与する) でもキー名が ``\\ufeffNOTION_SECRET`` のように
    壊れないようにする。
    """
    values: "dict[str, str]" = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        parsed = _parse_env_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        values[key] = value
    return values


def _parse_env_line(line: str) -> tuple[str, str] | None:
    """1行を `(key, value)` に変換。コメント・空行・不正形式は None。"""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if "=" not in stripped:
        return None

    key, _, raw_value = stripped.partition("=")
    key = key.strip()
    value = raw_value.strip()

    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]

    return key, value


def _project_root() -> Path:
    """プロジェクトルートを算出する (`src/ihealth/config.py` から 2 階層上)。"""
    return Path(__file__).resolve().parent.parent.parent
