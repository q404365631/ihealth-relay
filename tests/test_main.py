"""``ihealth.__main__`` の引数処理 / ヘルパーの回帰テスト.

Phase 1 A3-md で ``--publisher`` / ``--markdown-out`` を追加したので
その引数経路を smoke test する.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ihealth.__main__ import (
    _parse_args, _resolve_markdown_out, _resolve_slack_webhook,
    _resolve_sqlite_path, _resolve_target_date, main,
)


class TestResolveMarkdownOut(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(_resolve_markdown_out(None))

    def test_absolute_path_passthrough(self):
        result = _resolve_markdown_out("/tmp/obsidian")
        self.assertEqual(result, Path("/tmp/obsidian"))

    def test_tilde_slash_expanded(self):
        result = _resolve_markdown_out("~/obsidian")
        self.assertEqual(result, Path.home() / "obsidian")

    def test_relative_path_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _resolve_markdown_out("./obsidian")
        self.assertIn("絶対パス", str(ctx.exception))

    def test_tilde_user_form_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _resolve_markdown_out("~root/obsidian")
        self.assertIn("~/", str(ctx.exception))

    def test_blank_string_raises(self):
        # codex round 1 PR #23 指摘: 空文字 / 空白だけは fail-fast
        with self.assertRaises(ValueError) as ctx:
            _resolve_markdown_out("")
        self.assertIn("空文字", str(ctx.exception))
        with self.assertRaises(ValueError):
            _resolve_markdown_out("   ")

    def test_existing_file_rejected(self):
        # codex round 3 PR #23 nice-to-have: --markdown-out はディレクトリ限定
        with tempfile.NamedTemporaryFile(suffix=".txt") as fp:
            with self.assertRaises(ValueError) as ctx:
                _resolve_markdown_out(fp.name)
            self.assertIn("ファイル", str(ctx.exception))


class TestResolveSlackWebhook(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(_resolve_slack_webhook(None))

    def test_hooks_slack_com_passthrough(self):
        url = "https://hooks.slack.com/services/T0/B0/secret"
        self.assertEqual(_resolve_slack_webhook(url), url)

    def test_http_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            _resolve_slack_webhook("http://hooks.slack.com/services/x/y/z")
        self.assertIn("https://", str(ctx.exception))

    def test_arbitrary_host_rejected(self):
        # codex round 1 PR #24: SSRF 防止のため hooks.slack.com 以外は reject
        with self.assertRaises(ValueError) as ctx:
            _resolve_slack_webhook("https://evil.example.com/services/x/y/z")
        self.assertIn("host", str(ctx.exception).lower())

    def test_userinfo_rejected(self):
        with self.assertRaises(ValueError):
            _resolve_slack_webhook(
                "https://user:pass@hooks.slack.com/services/x/y/z"
            )

    def test_path_outside_services_rejected(self):
        with self.assertRaises(ValueError):
            _resolve_slack_webhook("https://hooks.slack.com/api/incoming/x")

    def test_query_rejected(self):
        with self.assertRaises(ValueError):
            _resolve_slack_webhook(
                "https://hooks.slack.com/services/x/y/z?token=secret"
            )

    def test_blank_rejected(self):
        with self.assertRaises(ValueError):
            _resolve_slack_webhook("")
        with self.assertRaises(ValueError):
            _resolve_slack_webhook("  ")

    def test_invalid_scheme_rejected(self):
        with self.assertRaises(ValueError):
            _resolve_slack_webhook("file:///tmp/secret")


class TestResolveSqlitePath(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(_resolve_sqlite_path(None))

    def test_absolute_path_passthrough(self):
        self.assertEqual(_resolve_sqlite_path("/tmp/h.db"), Path("/tmp/h.db"))

    def test_tilde_slash_expanded(self):
        self.assertEqual(_resolve_sqlite_path("~/h.db"), Path.home() / "h.db")

    def test_relative_rejected(self):
        with self.assertRaises(ValueError):
            _resolve_sqlite_path("./h.db")

    def test_tilde_user_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            _resolve_sqlite_path("~root/h.db")
        self.assertIn("--sqlite-path", str(ctx.exception))

    def test_blank_string_rejected(self):
        with self.assertRaises(ValueError):
            _resolve_sqlite_path("")
        with self.assertRaises(ValueError):
            _resolve_sqlite_path("  ")

    def test_existing_directory_rejected(self):
        # codex round 2 PR #23 指摘: CLI 側にも file-path 意味論
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                _resolve_sqlite_path(tmp)
            self.assertIn("ディレクトリ", str(ctx.exception))


class TestResolveTargetDate(unittest.TestCase):
    """既存挙動の回帰防止 (Phase 1 A3-md で touch しない)."""

    def test_iso_date_parsed(self):
        from datetime import date as _date
        self.assertEqual(_resolve_target_date("2026-04-22"), _date(2026, 4, 22))

    def test_invalid_date_raises_value_error(self):
        with self.assertRaises(ValueError):
            _resolve_target_date("not-a-date")


class TestParseArgs(unittest.TestCase):
    """argparse の経路: ``--publisher`` / ``--markdown-out`` の追加."""

    def test_default_publisher_is_notion(self):
        args = _parse_args([])
        self.assertEqual(args.publisher, "notion")

    def test_publisher_markdown(self):
        args = _parse_args(["--publisher", "markdown"])
        self.assertEqual(args.publisher, "markdown")

    def test_publisher_dryrun(self):
        args = _parse_args(["--publisher", "dryrun"])
        self.assertEqual(args.publisher, "dryrun")

    def test_publisher_stdout(self):
        args = _parse_args(["--publisher", "stdout"])
        self.assertEqual(args.publisher, "stdout")

    def test_publisher_sqlite(self):
        args = _parse_args(["--publisher", "sqlite"])
        self.assertEqual(args.publisher, "sqlite")

    def test_publisher_slack(self):
        args = _parse_args(["--publisher", "slack"])
        self.assertEqual(args.publisher, "slack")

    def test_slack_webhook_passthrough(self):
        url = "https://hooks.slack.com/services/T0/B0/x"
        args = _parse_args(["--slack-webhook", url])
        self.assertEqual(args.slack_webhook, url)

    def test_slack_webhook_default_none(self):
        args = _parse_args([])
        self.assertIsNone(args.slack_webhook)

    def test_sqlite_path_passthrough(self):
        args = _parse_args(["--sqlite-path", "/tmp/h.db"])
        self.assertEqual(args.sqlite_path, "/tmp/h.db")

    def test_sqlite_path_default_none(self):
        args = _parse_args([])
        self.assertIsNone(args.sqlite_path)

    def test_publisher_invalid_choice_exits(self):
        # argparse は invalid choice で SystemExit (exit_code=2)
        with self.assertRaises(SystemExit) as ctx:
            _parse_args(["--publisher", "bogus"])
        self.assertEqual(ctx.exception.code, 2)

    def test_markdown_out_passthrough(self):
        args = _parse_args(["--markdown-out", "/tmp/x"])
        self.assertEqual(args.markdown_out, "/tmp/x")

    def test_markdown_out_default_none(self):
        args = _parse_args([])
        self.assertIsNone(args.markdown_out)

    def test_dry_run_and_publisher_coexist(self):
        # CLI レベルでは両方受け入れる (workflow 層で dry_run が勝つ)
        args = _parse_args(["--dry-run", "--publisher", "markdown"])
        self.assertTrue(args.dry_run)
        self.assertEqual(args.publisher, "markdown")


class TestMainEndToEnd(unittest.TestCase):
    """``main(argv)`` の end-to-end smoke test.

    fetcher / parser / publisher を具体的にモックして、 main() の終了コード /
    Reminders 経路までを 1 関数呼び出しで検証する.
    """

    def _setup_env(self, tmp: Path, *, with_notion: bool = True) -> Path:
        """テスト用の .env と HEALTH_EXPORT_DIR を作って返す.

        ``with_notion=False`` で markdown publisher 専用の最小 .env を組む.
        """
        health = tmp / "AutoSync"
        health.mkdir()
        env_path = tmp / ".env"
        lines = [
            f"HEALTH_EXPORT_DIR={health}",
            "BIRTH_DATE=1990-01-01",
        ]
        if with_notion:
            lines.insert(0, "NOTION_SECRET=t")
            lines.insert(1, "DATABASE_ID=db")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return env_path

    def test_dry_run_without_notion_secret_returns_0(self):
        """``--dry-run`` は NOTION_SECRET 不在でも exit 0 で終わる."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_path = self._setup_env(tmp_path, with_notion=False)

            with patch("ihealth.config._project_root", return_value=tmp_path), \
                 patch("ihealth.workflow.fetch_all_metrics") as mock_fetch, \
                 patch("ihealth.workflow.parse_all") as mock_parse:
                from datetime import date as _date
                from ihealth.models import DailyHealthData
                from ihealth.source import FetchResult
                mock_fetch.return_value = FetchResult(
                    target_date=_date(2026, 4, 22),
                    archive_dir=tmp_path / "archive" / "2026-04-22",
                    archived={}, missing=[],
                )
                mock_parse.return_value = DailyHealthData(
                    date=_date(2026, 4, 22), step_count=100,
                )
                rc = main(["--dry-run", "--date", "2026-04-22"])
            self.assertEqual(rc, 0)

    def test_markdown_without_notion_secret_writes_file(self):
        """``--publisher markdown`` は NOTION_SECRET 不在でも書き込み完了."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_path = self._setup_env(tmp_path, with_notion=False)
            md_out = tmp_path / "obsidian"

            with patch("ihealth.config._project_root", return_value=tmp_path), \
                 patch("ihealth.workflow.fetch_all_metrics") as mock_fetch, \
                 patch("ihealth.workflow.parse_all") as mock_parse:
                from datetime import date as _date
                from ihealth.models import DailyHealthData
                from ihealth.source import FetchResult
                mock_fetch.return_value = FetchResult(
                    target_date=_date(2026, 4, 22),
                    archive_dir=tmp_path / "archive" / "2026-04-22",
                    archived={}, missing=[],
                )
                mock_parse.return_value = DailyHealthData(
                    date=_date(2026, 4, 22), step_count=12345,
                )
                rc = main([
                    "--publisher", "markdown",
                    "--markdown-out", str(md_out),
                    "--date", "2026-04-22",
                ])
            self.assertEqual(rc, 0)
            written = md_out / "2026-04-22.md"
            self.assertTrue(written.is_file())
            content = written.read_text(encoding="utf-8")
            self.assertIn("step_count: 12345", content)

    def test_markdown_publish_error_returns_exit_7(self):
        """書き込み失敗時は exit code 7 (MarkdownAppError).

        codex round 3 PR #23 で --markdown-out が「ディレクトリ限定」になったので
        既存ファイル指定ではなく、有効なディレクトリを指定 + その中に
        ``2026-04-22.md`` という名前の **ディレクトリ** を作って ``os.replace``
        を失敗させる経路で検証する.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_path = self._setup_env(tmp_path, with_notion=False)
            md_out = tmp_path / "obsidian"
            md_out.mkdir()
            # 出力ファイル名と同じ名前のディレクトリを置く → os.replace 失敗
            (md_out / "2026-04-22.md").mkdir()

            with patch("ihealth.config._project_root", return_value=tmp_path), \
                 patch("ihealth.workflow.fetch_all_metrics") as mock_fetch, \
                 patch("ihealth.workflow.parse_all") as mock_parse:
                from datetime import date as _date
                from ihealth.models import DailyHealthData
                from ihealth.source import FetchResult
                mock_fetch.return_value = FetchResult(
                    target_date=_date(2026, 4, 22),
                    archive_dir=tmp_path / "archive" / "2026-04-22",
                    archived={}, missing=[],
                )
                mock_parse.return_value = DailyHealthData(
                    date=_date(2026, 4, 22), step_count=100,
                )
                # notify_failure は side-effect なしで返るように mock
                with patch("ihealth.__main__.notify_failure"):
                    rc = main([
                        "--publisher", "markdown",
                        "--markdown-out", str(md_out),
                        "--date", "2026-04-22",
                    ])
            self.assertEqual(rc, 7)

    def test_stdout_publisher_writes_json_to_stdout(self):
        """``--publisher stdout`` で JSON 1 行を stdout に書き、 exit 0."""
        import io
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._setup_env(tmp_path, with_notion=False)

            captured = io.StringIO()
            with patch("ihealth.config._project_root", return_value=tmp_path), \
                 patch("ihealth.workflow.fetch_all_metrics") as mock_fetch, \
                 patch("ihealth.workflow.parse_all") as mock_parse, \
                 patch("sys.stdout", captured):
                from datetime import date as _date
                from ihealth.models import DailyHealthData
                from ihealth.source import FetchResult
                mock_fetch.return_value = FetchResult(
                    target_date=_date(2026, 4, 22),
                    archive_dir=tmp_path / "archive" / "2026-04-22",
                    archived={}, missing=[],
                )
                mock_parse.return_value = DailyHealthData(
                    date=_date(2026, 4, 22), step_count=12345,
                )
                rc = main([
                    "--publisher", "stdout",
                    "--date", "2026-04-22",
                ])
            self.assertEqual(rc, 0)
            output = captured.getvalue()
            # JSON 1 行
            import json as _json
            parsed = _json.loads(output)
            self.assertEqual(parsed["date"], "2026-04-22")
            self.assertEqual(parsed["step_count"], 12345)

    def test_sqlite_publisher_writes_db(self):
        """``--publisher sqlite --sqlite-path`` で DB 作成 + 1 行 insert + exit 0."""
        import sqlite3 as _sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._setup_env(tmp_path, with_notion=False)
            db_path = tmp_path / "h.db"

            with patch("ihealth.config._project_root", return_value=tmp_path), \
                 patch("ihealth.workflow.fetch_all_metrics") as mock_fetch, \
                 patch("ihealth.workflow.parse_all") as mock_parse:
                from datetime import date as _date
                from ihealth.models import DailyHealthData
                from ihealth.source import FetchResult
                mock_fetch.return_value = FetchResult(
                    target_date=_date(2026, 4, 22),
                    archive_dir=tmp_path / "archive" / "2026-04-22",
                    archived={}, missing=[],
                )
                mock_parse.return_value = DailyHealthData(
                    date=_date(2026, 4, 22), step_count=12345,
                )
                rc = main([
                    "--publisher", "sqlite",
                    "--sqlite-path", str(db_path),
                    "--date", "2026-04-22",
                ])
            self.assertEqual(rc, 0)
            self.assertTrue(db_path.is_file())
            with _sqlite3.connect(str(db_path)) as conn:
                row = conn.execute(
                    "SELECT step_count FROM daily_health WHERE date = ?",
                    ("2026-04-22",),
                ).fetchone()
            self.assertEqual(row[0], 12345)

    def test_slack_publisher_posts_message(self):
        """``--publisher slack --slack-webhook`` で webhook に POST + exit 0."""
        from unittest.mock import MagicMock, patch as _patch
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._setup_env(tmp_path, with_notion=False)

            fake_resp = MagicMock()
            fake_resp.__enter__ = MagicMock(return_value=fake_resp)
            fake_resp.__exit__ = MagicMock(return_value=False)
            fake_resp.status = 200
            fake_resp.read = MagicMock(return_value=b"ok")

            with _patch("ihealth.config._project_root", return_value=tmp_path), \
                 _patch("ihealth.workflow.fetch_all_metrics") as mock_fetch, \
                 _patch("ihealth.workflow.parse_all") as mock_parse, \
                 _patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
                from datetime import date as _date
                from ihealth.models import DailyHealthData
                from ihealth.source import FetchResult
                mock_fetch.return_value = FetchResult(
                    target_date=_date(2026, 4, 22),
                    archive_dir=tmp_path / "archive" / "2026-04-22",
                    archived={}, missing=[],
                )
                mock_parse.return_value = DailyHealthData(
                    date=_date(2026, 4, 22), step_count=12345,
                )
                rc = main([
                    "--publisher", "slack",
                    "--slack-webhook", "https://hooks.slack.com/services/T0/B0/x",
                    "--date", "2026-04-22",
                ])
            self.assertEqual(rc, 0)
            self.assertEqual(mock_open.call_count, 1)

    def test_notion_publisher_missing_secret_returns_exit_3(self):
        """``--publisher notion`` (default) で NOTION_SECRET 欠落 → exit 3."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env_path = self._setup_env(tmp_path, with_notion=False)
            with patch("ihealth.config._project_root", return_value=tmp_path), \
                 patch("ihealth.__main__.notify_failure"):
                rc = main(["--date", "2026-04-22"])
            self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()
