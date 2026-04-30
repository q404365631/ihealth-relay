"""``ihealth.config`` の回帰テスト.

Phase 1 A3-md で ``MARKDOWN_OUTPUT_DIR`` を ``Config.markdown_output_dir`` に
読み込む経路を追加したので、その単体テスト + ``HEALTH_EXPORT_DIR`` 系の挙動を
カバーする (``_parse_optional_dir_path`` 共通化の回帰防止).
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from ihealth.config import Config, ConfigError, load_config


def _write_env(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _minimum_env(
    health_export_dir: Path,
    *,
    extra: str = "",
) -> str:
    return (
        f'NOTION_SECRET=secret_token\n'
        f'DATABASE_ID=db_id\n'
        f'HEALTH_EXPORT_DIR={health_export_dir}\n'
        f'BIRTH_DATE=1990-01-01\n'
        f'{extra}'
    )


class TestLoadConfigBasics(unittest.TestCase):
    """既存挙動の回帰防止."""

    def test_loads_required_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(env_path, _minimum_env(health))
            cfg = load_config(env_path)
        self.assertEqual(cfg.notion_secret, "secret_token")
        self.assertEqual(cfg.database_id, "db_id")
        self.assertEqual(cfg.birth_date, date(1990, 1, 1))
        self.assertIsNone(cfg.markdown_output_dir)  # 既定 None

    def test_missing_env_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                load_config(Path(tmp) / "nonexistent.env")

    def test_missing_required_key_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                'NOTION_SECRET=t\nDATABASE_ID=d\nBIRTH_DATE=1990-01-01\n',
            )  # HEALTH_EXPORT_DIR 欠落
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("HEALTH_EXPORT_DIR", str(ctx.exception))


class TestRequireNotionFlag(unittest.TestCase):
    """Phase 1 A3-md: ``--publisher markdown`` で Notion キー不要 (codex blocking #2)."""

    def test_default_requires_notion(self):
        # 既定 require_notion=True で NOTION_SECRET 欠落 → ConfigError
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                f"DATABASE_ID=db\nHEALTH_EXPORT_DIR={health}\nBIRTH_DATE=1990-01-01\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("NOTION_SECRET", str(ctx.exception))

    def test_require_notion_false_allows_missing_secret(self):
        # require_notion=False なら NOTION_SECRET 欠落でも通る
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                f"HEALTH_EXPORT_DIR={health}\nBIRTH_DATE=1990-01-01\n",
            )
            cfg = load_config(env_path, require_notion=False)
            self.assertIsNone(cfg.notion_secret)
            self.assertIsNone(cfg.database_id)

    def test_require_notion_false_still_requires_health_dir(self):
        # require_notion=False でも HEALTH_EXPORT_DIR は必須
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_path = tmp_path / ".env"
            _write_env(env_path, "BIRTH_DATE=1990-01-01\n")
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path, require_notion=False)
            self.assertIn("HEALTH_EXPORT_DIR", str(ctx.exception))

    def test_require_notion_false_still_requires_birth_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(env_path, f"HEALTH_EXPORT_DIR={health}\n")
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path, require_notion=False)
            self.assertIn("BIRTH_DATE", str(ctx.exception))

    def test_require_notion_false_keeps_secrets_when_present(self):
        # require_notion=False でも .env に値があれば読み込む
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(health),
            )
            cfg = load_config(env_path, require_notion=False)
            self.assertEqual(cfg.notion_secret, "secret_token")
            self.assertEqual(cfg.database_id, "db_id")


class TestMarkdownOutputDir(unittest.TestCase):
    """Phase 1 A3-md: ``MARKDOWN_OUTPUT_DIR`` の optional 読み込み."""

    def test_unset_yields_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(env_path, _minimum_env(health))
            cfg = load_config(env_path)
        self.assertIsNone(cfg.markdown_output_dir)

    def test_empty_string_yields_none(self):
        # 空文字列は "未指定" 扱い (KEY= だけ書いてある場合)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(health, extra="MARKDOWN_OUTPUT_DIR=\n"),
            )
            cfg = load_config(env_path)
        self.assertIsNone(cfg.markdown_output_dir)

    def test_absolute_path_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            md_dir = tmp_path / "obsidian"
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(
                    health, extra=f"MARKDOWN_OUTPUT_DIR={md_dir}\n",
                ),
            )
            cfg = load_config(env_path)
        self.assertEqual(cfg.markdown_output_dir, md_dir)

    def test_tilde_slash_expanded(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(health, extra="MARKDOWN_OUTPUT_DIR=~/obsidian-md\n"),
            )
            cfg = load_config(env_path)
        self.assertEqual(cfg.markdown_output_dir, Path.home() / "obsidian-md")

    def test_relative_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(health, extra="MARKDOWN_OUTPUT_DIR=./obsidian\n"),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("MARKDOWN_OUTPUT_DIR", str(ctx.exception))
            self.assertIn("絶対パス", str(ctx.exception))

    def test_tilde_user_form_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(
                    health, extra="MARKDOWN_OUTPUT_DIR=~root/obsidian\n",
                ),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("MARKDOWN_OUTPUT_DIR", str(ctx.exception))

    def test_existing_file_rejected(self):
        # codex round 3 PR #23 nice-to-have: ディレクトリ限定
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            md_as_file = tmp_path / "actually_a_file.txt"
            md_as_file.write_text("x", encoding="utf-8")
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(
                    health, extra=f"MARKDOWN_OUTPUT_DIR={md_as_file}\n",
                ),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("MARKDOWN_OUTPUT_DIR", str(ctx.exception))
            self.assertIn("ファイル", str(ctx.exception))


class TestSQLiteDbPath(unittest.TestCase):
    """Phase 1 A3-sqlite: ``SQLITE_DB_PATH`` の optional 読み込み."""

    def test_unset_yields_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(env_path, _minimum_env(health))
            cfg = load_config(env_path)
        self.assertIsNone(cfg.sqlite_db_path)

    def test_absolute_path_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            db_path = tmp_path / "health.db"
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(health, extra=f"SQLITE_DB_PATH={db_path}\n"),
            )
            cfg = load_config(env_path)
        self.assertEqual(cfg.sqlite_db_path, db_path)

    def test_relative_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(health, extra="SQLITE_DB_PATH=./h.db\n"),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("SQLITE_DB_PATH", str(ctx.exception))

    def test_existing_directory_rejected(self):
        # codex round 1 PR #23 指摘: 既存ディレクトリ → ConfigError
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            db_dir = tmp_path / "is_a_dir"
            db_dir.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(health, extra=f"SQLITE_DB_PATH={db_dir}\n"),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("SQLITE_DB_PATH", str(ctx.exception))
            self.assertIn("ディレクトリ", str(ctx.exception))

    def test_nonexistent_path_accepted(self):
        # 初回起動 (DB 新規作成) は path 存在不要
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            db_path = tmp_path / "future" / "h.db"  # 存在しない
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(health, extra=f"SQLITE_DB_PATH={db_path}\n"),
            )
            cfg = load_config(env_path)
            self.assertEqual(cfg.sqlite_db_path, db_path)


class TestSlackWebhookUrl(unittest.TestCase):
    """Phase 1 A3-slack: ``SLACK_WEBHOOK_URL`` の optional 読み込み."""

    def test_unset_yields_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(env_path, _minimum_env(health))
            cfg = load_config(env_path)
        self.assertIsNone(cfg.slack_webhook_url)

    def test_https_url_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            url = "https://hooks.slack.com/services/T0/B0/secret"
            _write_env(
                env_path,
                _minimum_env(health, extra=f"SLACK_WEBHOOK_URL={url}\n"),
            )
            cfg = load_config(env_path)
        self.assertEqual(cfg.slack_webhook_url, url)

    def test_http_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(
                    health,
                    extra="SLACK_WEBHOOK_URL=http://hooks.slack.com/services/x/y/z\n",
                ),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("SLACK_WEBHOOK_URL", str(ctx.exception))
            self.assertIn("https://", str(ctx.exception))

    def test_arbitrary_host_rejected(self):
        # codex round 1 PR #24: SSRF 防止
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            health = tmp_path / "AutoSync"
            health.mkdir()
            env_path = tmp_path / ".env"
            _write_env(
                env_path,
                _minimum_env(
                    health,
                    extra="SLACK_WEBHOOK_URL=https://evil.example.com/services/x/y/z\n",
                ),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_path)
            self.assertIn("SLACK_WEBHOOK_URL", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
