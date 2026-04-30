"""workflow.run_pipeline / Publisher 抽象の回帰テスト (Issue #16)。

依存性注入 (``PipelineDeps``) により、parser / fetcher / publisher をモックに
差し替えて **ネットワーク・ファイルなしで** パイプライン全体の挙動を検証する。
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from ihealth.config import Config
from ihealth.errors import (
    AppError,
    HealthExportDirError,
    NotionAppError,
    SourceAppError,
)
from ihealth.models import DailyHealthData
from ihealth.source import FetchResult, SourceError
from ihealth.workflow import (
    DryRunPublisher,
    NotionPublisher,
    PipelineDeps,
    Publisher,
    run_pipeline,
)


def _make_config(health_export_dir: Path, archive_root: Path) -> Config:
    return Config(
        notion_secret="t",
        database_id="db",
        health_export_dir=health_export_dir,
        archive_root=archive_root,
        birth_date=date(1990, 1, 1),
        log_level="info",
    )


def _logger() -> logging.Logger:
    return logging.getLogger("test-workflow")


class _FakePublisher:
    def __init__(self) -> None:
        self.published: "list[DailyHealthData]" = []

    def publish(self, health_data: DailyHealthData) -> None:
        self.published.append(health_data)


class _FakeRaisingPublisher:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def publish(self, health_data: DailyHealthData) -> None:
        raise self._exc


class TestRunPipeline(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.health_dir = self.root / "AutoSync"
        self.health_dir.mkdir()
        self.archive_root = self.root / "archive"
        self.archive_root.mkdir()
        self.config = _make_config(self.health_dir, self.archive_root)
        self.target_date = date(2026, 4, 22)

    def tearDown(self):
        self._tmp.cleanup()

    def _deps(
        self,
        *,
        fetcher=None,
        parser=None,
        publisher=None,
        sleep_payload_builder=None,
    ) -> PipelineDeps:
        fetch_result = FetchResult(
            target_date=self.target_date,
            archive_dir=self.archive_root / self.target_date.isoformat(),
            archived={},
            missing=[],
        )
        return PipelineDeps(
            fetcher=fetcher or (lambda **kwargs: fetch_result),
            parser=parser or (
                lambda *args, **kwargs: DailyHealthData(date=self.target_date, step_count=100)
            ),
            publisher=publisher or _FakePublisher(),
            sleep_payload_builder=sleep_payload_builder or (lambda *a, **kw: {}),
        )

    def test_health_export_dir_missing_raises_app_error(self):
        self.config = _make_config(self.root / "nonexistent", self.archive_root)
        with self.assertRaises(HealthExportDirError) as ctx:
            run_pipeline(self.config, self.target_date, self._deps(), _logger())
        self.assertEqual(ctx.exception.exit_code, 4)

    def test_health_export_dir_not_directory_raises(self):
        # 存在はするがファイル (非ディレクトリ)
        fake_file = self.root / "just_a_file"
        fake_file.write_text("x")
        self.config = _make_config(fake_file, self.archive_root)
        with self.assertRaises(HealthExportDirError):
            run_pipeline(self.config, self.target_date, self._deps(), _logger())

    def test_source_error_wrapped_to_source_app_error(self):
        def raising_fetcher(**kwargs):
            raise SourceError("fetch failed")
        with self.assertRaises(SourceAppError) as ctx:
            run_pipeline(
                self.config,
                self.target_date,
                self._deps(fetcher=raising_fetcher),
                _logger(),
            )
        self.assertEqual(ctx.exception.exit_code, 5)
        self.assertIn("データ未到着", ctx.exception.title)

    def test_happy_path_invokes_publisher(self):
        pub = _FakePublisher()
        result = run_pipeline(
            self.config, self.target_date, self._deps(publisher=pub), _logger(),
        )
        self.assertIsInstance(result, DailyHealthData)
        self.assertEqual(len(pub.published), 1)
        self.assertEqual(pub.published[0].step_count, 100)

    def test_sleep_payload_builder_is_called_with_fetch_result(self):
        captured_args: "list[tuple]" = []
        def builder(config, fetch_result, target_date, logger):
            captured_args.append((config, fetch_result, target_date))
            return {}
        run_pipeline(
            self.config,
            self.target_date,
            self._deps(sleep_payload_builder=builder),
            _logger(),
        )
        self.assertEqual(len(captured_args), 1)
        self.assertEqual(captured_args[0][0], self.config)
        self.assertEqual(captured_args[0][2], self.target_date)


class TestDryRunPublisher(unittest.TestCase):
    def test_publish_does_not_raise(self):
        log = MagicMock()
        pub = DryRunPublisher(logger=log)
        data = DailyHealthData(date=date(2026, 4, 22), step_count=100)
        pub.publish(data)
        # ログに何らかの出力が出る
        self.assertTrue(log.info.called)


class TestNotionPublisher(unittest.TestCase):
    def setUp(self):
        self.log = MagicMock()
        self.client = MagicMock()
        self.target_date = date(2026, 4, 22)
        self.pub = NotionPublisher(
            client=self.client, target_date=self.target_date, logger=self.log,
        )

    def test_happy_path_calls_update_page(self):
        self.client.fetch_database_schema.return_value = {
            "Step Count": "number",
        }
        self.client.find_page_by_date.return_value = "page123"
        data = DailyHealthData(date=self.target_date, step_count=100)
        self.pub.publish(data)
        self.client.update_page.assert_called_once()
        args, _ = self.client.update_page.call_args
        self.assertEqual(args[0], "page123")
        self.assertIn("Step Count", args[1])

    def test_page_not_found_raises_notion_app_error(self):
        self.client.fetch_database_schema.return_value = {"Step Count": "number"}
        self.client.find_page_by_date.return_value = None
        data = DailyHealthData(date=self.target_date, step_count=100)
        with self.assertRaises(NotionAppError) as ctx:
            self.pub.publish(data)
        self.assertEqual(ctx.exception.exit_code, 6)
        self.assertIn("日記ページ", ctx.exception.title)

    def test_notion_error_wrapped_to_app_error(self):
        from ihealth.publishers.notion import NotionError
        self.client.fetch_database_schema.side_effect = NotionError(
            "HTTP 500", status=500, response_body="{}",
        )
        data = DailyHealthData(date=self.target_date, step_count=100)
        with self.assertRaises(NotionAppError) as ctx:
            self.pub.publish(data)
        self.assertEqual(ctx.exception.exit_code, 6)
        self.assertIn("HTTP 500", ctx.exception.title)

    def test_empty_properties_with_no_populated_returns_without_update(self):
        # populated 0 + props 空 → 警告だけ返して update_page 呼ばない
        # (= 計測データなしの日は何もしないのが正しい挙動)
        self.client.fetch_database_schema.return_value = {}
        self.client.find_page_by_date.return_value = "page123"
        data = DailyHealthData(date=self.target_date)  # 全フィールド None
        self.pub.publish(data)
        self.client.update_page.assert_not_called()

    def test_populated_but_props_empty_raises_notion_app_error(self):
        # codex round 1 PR #26 指摘 (silent data loss 防止):
        # populated > 0 なのに props 空 (= DB 列名と PROPERTY_MAP / config.toml
        # の両方が一致しない) → fail-fast で NotionAppError(6) を上げる
        self.client.fetch_database_schema.return_value = {"Some Other Column": "number"}
        self.client.find_page_by_date.return_value = "page123"
        data = DailyHealthData(date=self.target_date, step_count=100)
        with self.assertRaises(NotionAppError) as ctx:
            self.pub.publish(data)
        self.assertEqual(ctx.exception.exit_code, 6)
        self.assertIn("列名", ctx.exception.title)
        self.client.update_page.assert_not_called()


class TestAppErrorHierarchy(unittest.TestCase):
    def test_each_error_has_distinct_exit_code(self):
        from ihealth.errors import (
            ConfigAppError,
            HealthExportDirError,
            MarkdownAppError,
            NotionAppError,
            SlackAppError,
            SourceAppError,
            SQLiteAppError,
            StdoutAppError,
        )
        codes = [
            ConfigAppError("x").exit_code,
            HealthExportDirError("x").exit_code,
            SourceAppError("x").exit_code,
            NotionAppError("x").exit_code,
            MarkdownAppError("x").exit_code,
            StdoutAppError("x").exit_code,
            SQLiteAppError("x").exit_code,
            SlackAppError("x").exit_code,
        ]
        # 3, 4, 5, 6, 7, 8, 9, 10 で衝突なし
        self.assertEqual(sorted(codes), [3, 4, 5, 6, 7, 8, 9, 10])

    def test_user_message_defaults_to_arg(self):
        err = AppError("failure")
        self.assertEqual(err.user_message, "failure")
        self.assertEqual(err.title, "failure")
        self.assertEqual(err.body, "failure")

    def test_title_and_body_can_differ(self):
        err = AppError("x", title="T", body="B")
        self.assertEqual(err.title, "T")
        self.assertEqual(err.body, "B")
        self.assertEqual(err.user_message, "x")


class TestLoadNotionPropertyOverrides(unittest.TestCase):
    """workflow._load_notion_property_overrides の fail-fast 挙動.

    codex review 2026-04-29 の指摘 #1 (silent data loss 防止) に対応.
    "config.toml 不在 → fail-soft", "存在するが壊れている → fail-fast".
    """

    def setUp(self):
        from ihealth import workflow
        self._workflow = workflow
        self._logger = logging.getLogger("test-load-overrides")
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        # Path(__file__).resolve().parent.parent.parent をテスト用ディレクトリに
        # 差し替えるため find_config_path を patch する
        from unittest.mock import patch
        self._patcher = patch(
            "ihealth.config_file.find_config_path",
            side_effect=lambda root: (
                self.tmp_path / "config.toml"
                if (self.tmp_path / "config.toml").is_file() else None
            ),
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def test_returns_empty_when_config_absent(self):
        """config.toml 不在 → 空 dict (fail-soft)."""
        result = self._workflow._load_notion_property_overrides(self._logger)
        self.assertEqual(result, {})

    def test_loads_valid_overrides(self):
        (self.tmp_path / "config.toml").write_text(
            '[publishers.notion.fields.step_count]\nproperty = "歩数 (歩)"\n',
            encoding="utf-8",
        )
        result = self._workflow._load_notion_property_overrides(self._logger)
        self.assertEqual(result, {"step_count": "歩数 (歩)"})

    def test_raises_on_toml_parse_error(self):
        from ihealth.errors import ConfigAppError
        (self.tmp_path / "config.toml").write_text(
            "broken === toml\n", encoding="utf-8",
        )
        with self.assertRaises(ConfigAppError) as ctx:
            self._workflow._load_notion_property_overrides(self._logger)
        self.assertIn("config.toml", str(ctx.exception))

    def test_raises_on_unknown_field_typo(self):
        """typo した field 名は silent ignore せず ConfigAppError (codex 指摘)."""
        from ihealth.errors import ConfigAppError
        (self.tmp_path / "config.toml").write_text(
            '[publishers.notion.fields.step_count_typo]\nproperty = "x"\n',
            encoding="utf-8",
        )
        with self.assertRaises(ConfigAppError) as ctx:
            self._workflow._load_notion_property_overrides(self._logger)
        self.assertIn("step_count_typo", str(ctx.exception))

    def test_raises_on_property_collision(self):
        """異なる field が同じ Notion プロパティ名を指したら ConfigAppError (codex 指摘)."""
        from ihealth.errors import ConfigAppError
        (self.tmp_path / "config.toml").write_text(
            '[publishers.notion.fields.step_count]\n'
            'property = "Same Name"\n'
            '[publishers.notion.fields.distance_km]\n'
            'property = "Same Name"\n',
            encoding="utf-8",
        )
        with self.assertRaises(ConfigAppError) as ctx:
            self._workflow._load_notion_property_overrides(self._logger)
        self.assertIn("衝突", str(ctx.exception))
        self.assertIn("Same Name", str(ctx.exception))

    def test_raises_on_collision_with_default(self):
        """override 後の名前が DEFAULT の他フィールドと衝突しても検出される."""
        from ihealth.errors import ConfigAppError
        # step_count を distance_km の DEFAULT 名 "Distance (km)" にすると衝突
        (self.tmp_path / "config.toml").write_text(
            '[publishers.notion.fields.step_count]\nproperty = "Distance (km)"\n',
            encoding="utf-8",
        )
        with self.assertRaises(ConfigAppError):
            self._workflow._load_notion_property_overrides(self._logger)


class TestBuildDefaultDepsPublisherKind(unittest.TestCase):
    """``build_default_deps`` の publisher_kind 分岐 (Phase 1 A3-md)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.health_dir = self.root / "AutoSync"
        self.health_dir.mkdir()
        self.archive_root = self.root / "archive"
        self.archive_root.mkdir()
        self.target_date = date(2026, 4, 22)
        self.log = logging.getLogger("test-build-deps")

    def tearDown(self):
        self._tmp.cleanup()

    def _config(
        self, *, markdown_output_dir: "Path | None" = None,
    ) -> Config:
        return Config(
            notion_secret="t",
            database_id="db",
            health_export_dir=self.health_dir,
            archive_root=self.archive_root,
            birth_date=date(1990, 1, 1),
            log_level="info",
            markdown_output_dir=markdown_output_dir,
        )

    def test_default_publisher_is_notion(self):
        from ihealth.workflow import build_default_deps
        deps = build_default_deps(
            self._config(),
            dry_run=False, target_date=self.target_date, logger=self.log,
        )
        self.assertIsInstance(deps.publisher, NotionPublisher)

    def test_dry_run_overrides_to_dry_run_publisher(self):
        from ihealth.workflow import build_default_deps
        deps = build_default_deps(
            self._config(),
            dry_run=True, target_date=self.target_date, logger=self.log,
            publisher_kind="markdown",  # dry_run が勝つ
            markdown_output_dir_override=self.root / "md_out",
        )
        self.assertIsInstance(deps.publisher, DryRunPublisher)

    def test_publisher_kind_dryrun_explicit(self):
        from ihealth.workflow import build_default_deps
        deps = build_default_deps(
            self._config(),
            dry_run=False, target_date=self.target_date, logger=self.log,
            publisher_kind="dryrun",
        )
        self.assertIsInstance(deps.publisher, DryRunPublisher)

    def test_publisher_kind_markdown_with_override(self):
        from ihealth.workflow import build_default_deps
        from ihealth.publishers.markdown import MarkdownPublisher
        deps = build_default_deps(
            self._config(),
            dry_run=False, target_date=self.target_date, logger=self.log,
            publisher_kind="markdown",
            markdown_output_dir_override=self.root / "md_out",
        )
        self.assertIsInstance(deps.publisher, MarkdownPublisher)

    def test_publisher_kind_markdown_with_config(self):
        from ihealth.workflow import build_default_deps
        from ihealth.publishers.markdown import MarkdownPublisher
        deps = build_default_deps(
            self._config(markdown_output_dir=self.root / "from_env"),
            dry_run=False, target_date=self.target_date, logger=self.log,
            publisher_kind="markdown",
        )
        self.assertIsInstance(deps.publisher, MarkdownPublisher)

    def test_override_wins_over_config(self):
        # CLI override が .env の MARKDOWN_OUTPUT_DIR より強い
        from ihealth.workflow import build_default_deps
        from ihealth.publishers.markdown import MarkdownPublisher
        cli_path = self.root / "from_cli"
        deps = build_default_deps(
            self._config(markdown_output_dir=self.root / "from_env"),
            dry_run=False, target_date=self.target_date, logger=self.log,
            publisher_kind="markdown",
            markdown_output_dir_override=cli_path,
        )
        self.assertIsInstance(deps.publisher, MarkdownPublisher)
        # internal _output_dir で確認
        self.assertEqual(deps.publisher._output_dir, cli_path)

    def test_publisher_kind_markdown_without_output_dir_raises(self):
        from ihealth.errors import ConfigAppError
        from ihealth.workflow import build_default_deps
        with self.assertRaises(ConfigAppError) as ctx:
            build_default_deps(
                self._config(markdown_output_dir=None),
                dry_run=False, target_date=self.target_date, logger=self.log,
                publisher_kind="markdown",
            )
        self.assertEqual(ctx.exception.exit_code, 3)
        self.assertIn("Markdown", ctx.exception.title)

    def test_unknown_publisher_kind_raises(self):
        from ihealth.errors import ConfigAppError
        from ihealth.workflow import build_default_deps
        with self.assertRaises(ConfigAppError):
            build_default_deps(
                self._config(),
                dry_run=False, target_date=self.target_date, logger=self.log,
                publisher_kind="bogus",
            )

    def test_notion_publisher_with_missing_secret_raises(self):
        # codex round 1 指摘 (blocking #2): require_notion=False で .env を読んだ
        # 後に notion publisher に切り替えるような操作ミスを検知する.
        from ihealth.errors import ConfigAppError
        from ihealth.workflow import build_default_deps
        config = Config(
            notion_secret=None,  # missing
            database_id=None,
            health_export_dir=self.health_dir,
            archive_root=self.archive_root,
            birth_date=date(1990, 1, 1),
            log_level="info",
        )
        with self.assertRaises(ConfigAppError) as ctx:
            build_default_deps(
                config,
                dry_run=False, target_date=self.target_date, logger=self.log,
                publisher_kind="notion",
            )
        self.assertEqual(ctx.exception.exit_code, 3)
        self.assertIn("NOTION_SECRET", str(ctx.exception))
        self.assertIn("DATABASE_ID", str(ctx.exception))

    def test_notion_publisher_partial_secret_raises(self):
        # NOTION_SECRET だけあって DATABASE_ID が欠落 → DATABASE_ID だけ含む
        from ihealth.errors import ConfigAppError
        from ihealth.workflow import build_default_deps
        config = Config(
            notion_secret="secret",
            database_id=None,
            health_export_dir=self.health_dir,
            archive_root=self.archive_root,
            birth_date=date(1990, 1, 1),
            log_level="info",
        )
        with self.assertRaises(ConfigAppError) as ctx:
            build_default_deps(
                config,
                dry_run=False, target_date=self.target_date, logger=self.log,
                publisher_kind="notion",
            )
        self.assertIn("DATABASE_ID", str(ctx.exception))
        self.assertNotIn("NOTION_SECRET", str(ctx.exception))

    def test_markdown_publisher_works_without_notion_secret(self):
        # markdown publisher は notion secret 無しで組める
        from ihealth.publishers.markdown import MarkdownPublisher
        from ihealth.workflow import build_default_deps
        config = Config(
            notion_secret=None,
            database_id=None,
            health_export_dir=self.health_dir,
            archive_root=self.archive_root,
            birth_date=date(1990, 1, 1),
            log_level="info",
        )
        deps = build_default_deps(
            config,
            dry_run=False, target_date=self.target_date, logger=self.log,
            publisher_kind="markdown",
            markdown_output_dir_override=self.root / "md_out",
        )
        self.assertIsInstance(deps.publisher, MarkdownPublisher)

    def test_dry_run_works_without_notion_secret(self):
        # dry-run は HTTP 不発射なので notion secret 無しで OK
        from ihealth.workflow import build_default_deps
        config = Config(
            notion_secret=None,
            database_id=None,
            health_export_dir=self.health_dir,
            archive_root=self.archive_root,
            birth_date=date(1990, 1, 1),
            log_level="info",
        )
        deps = build_default_deps(
            config,
            dry_run=True, target_date=self.target_date, logger=self.log,
        )
        self.assertIsInstance(deps.publisher, DryRunPublisher)

    def test_publisher_kind_stdout(self):
        from ihealth.publishers.stdout import StdoutPublisher
        from ihealth.workflow import build_default_deps
        deps = build_default_deps(
            self._config(),
            dry_run=False, target_date=self.target_date, logger=self.log,
            publisher_kind="stdout",
        )
        self.assertIsInstance(deps.publisher, StdoutPublisher)

    def test_stdout_publisher_works_without_notion_secret(self):
        from ihealth.publishers.stdout import StdoutPublisher
        from ihealth.workflow import build_default_deps
        config = Config(
            notion_secret=None,
            database_id=None,
            health_export_dir=self.health_dir,
            archive_root=self.archive_root,
            birth_date=date(1990, 1, 1),
            log_level="info",
        )
        deps = build_default_deps(
            config,
            dry_run=False, target_date=self.target_date, logger=self.log,
            publisher_kind="stdout",
        )
        self.assertIsInstance(deps.publisher, StdoutPublisher)

    def test_publisher_kind_sqlite_with_override(self):
        from ihealth.publishers.sqlite import SQLitePublisher
        from ihealth.workflow import build_default_deps
        deps = build_default_deps(
            self._config(),
            dry_run=False, target_date=self.target_date, logger=self.log,
            publisher_kind="sqlite",
            sqlite_db_path_override=self.root / "x.db",
        )
        self.assertIsInstance(deps.publisher, SQLitePublisher)

    def test_publisher_kind_sqlite_without_path_raises(self):
        from ihealth.errors import ConfigAppError
        from ihealth.workflow import build_default_deps
        with self.assertRaises(ConfigAppError) as ctx:
            build_default_deps(
                self._config(),
                dry_run=False, target_date=self.target_date, logger=self.log,
                publisher_kind="sqlite",
            )
        self.assertEqual(ctx.exception.exit_code, 3)
        self.assertIn("SQLite", ctx.exception.title)

    def test_sqlite_publisher_works_without_notion_secret(self):
        from ihealth.publishers.sqlite import SQLitePublisher
        from ihealth.workflow import build_default_deps
        config = Config(
            notion_secret=None,
            database_id=None,
            health_export_dir=self.health_dir,
            archive_root=self.archive_root,
            birth_date=date(1990, 1, 1),
            log_level="info",
        )
        deps = build_default_deps(
            config,
            dry_run=False, target_date=self.target_date, logger=self.log,
            publisher_kind="sqlite",
            sqlite_db_path_override=self.root / "x.db",
        )
        self.assertIsInstance(deps.publisher, SQLitePublisher)

    def test_publisher_kind_slack_with_override(self):
        from ihealth.publishers.slack import SlackPublisher
        from ihealth.workflow import build_default_deps
        deps = build_default_deps(
            self._config(),
            dry_run=False, target_date=self.target_date, logger=self.log,
            publisher_kind="slack",
            slack_webhook_url_override="https://hooks.slack.com/services/T0/B0/secret",
        )
        self.assertIsInstance(deps.publisher, SlackPublisher)

    def test_publisher_kind_slack_without_url_raises(self):
        from ihealth.errors import ConfigAppError
        from ihealth.workflow import build_default_deps
        with self.assertRaises(ConfigAppError) as ctx:
            build_default_deps(
                self._config(),
                dry_run=False, target_date=self.target_date, logger=self.log,
                publisher_kind="slack",
            )
        self.assertEqual(ctx.exception.exit_code, 3)
        self.assertIn("Slack", ctx.exception.title)

    def test_slack_publisher_works_without_notion_secret(self):
        from ihealth.publishers.slack import SlackPublisher
        from ihealth.workflow import build_default_deps
        config = Config(
            notion_secret=None,
            database_id=None,
            health_export_dir=self.health_dir,
            archive_root=self.archive_root,
            birth_date=date(1990, 1, 1),
            log_level="info",
        )
        deps = build_default_deps(
            config,
            dry_run=False, target_date=self.target_date, logger=self.log,
            publisher_kind="slack",
            slack_webhook_url_override="https://hooks.slack.com/services/T0/B0/secret",
        )
        self.assertIsInstance(deps.publisher, SlackPublisher)


class TestRunPipelineWrapsMarkdownError(unittest.TestCase):
    """``MarkdownPublishError`` が ``MarkdownAppError`` にラップされる."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.health_dir = self.root / "AutoSync"
        self.health_dir.mkdir()
        self.archive_root = self.root / "archive"
        self.archive_root.mkdir()
        self.target_date = date(2026, 4, 22)

    def tearDown(self):
        self._tmp.cleanup()

    def test_slack_publish_error_wrapped(self):
        from ihealth.errors import SlackAppError
        from ihealth.publishers.slack import SlackPublishError
        from ihealth.workflow import PipelineDeps, run_pipeline

        class _RaisingSlackPublisher:
            def publish(self, health_data):
                raise SlackPublishError("hooks.slack.com unreachable")

        config = Config(
            notion_secret="t",
            database_id="db",
            health_export_dir=self.health_dir,
            archive_root=self.archive_root,
            birth_date=date(1990, 1, 1),
            log_level="info",
        )
        fetch_result = FetchResult(
            target_date=self.target_date,
            archive_dir=self.archive_root / self.target_date.isoformat(),
            archived={}, missing=[],
        )
        deps = PipelineDeps(
            fetcher=lambda **kwargs: fetch_result,
            parser=lambda *a, **kw: DailyHealthData(date=self.target_date, step_count=100),
            publisher=_RaisingSlackPublisher(),
            sleep_payload_builder=lambda *a, **kw: {},
        )
        with self.assertRaises(SlackAppError) as ctx:
            run_pipeline(config, self.target_date, deps, logging.getLogger("t"))
        self.assertEqual(ctx.exception.exit_code, 10)
        self.assertIn("Slack", ctx.exception.title)

    def test_sqlite_publish_error_wrapped(self):
        from ihealth.errors import SQLiteAppError
        from ihealth.publishers.sqlite import SQLitePublishError
        from ihealth.workflow import PipelineDeps, run_pipeline

        class _RaisingSQLitePublisher:
            def publish(self, health_data):
                raise SQLitePublishError("disk full")

        config = Config(
            notion_secret="t",
            database_id="db",
            health_export_dir=self.health_dir,
            archive_root=self.archive_root,
            birth_date=date(1990, 1, 1),
            log_level="info",
        )
        fetch_result = FetchResult(
            target_date=self.target_date,
            archive_dir=self.archive_root / self.target_date.isoformat(),
            archived={}, missing=[],
        )
        deps = PipelineDeps(
            fetcher=lambda **kwargs: fetch_result,
            parser=lambda *a, **kw: DailyHealthData(date=self.target_date, step_count=100),
            publisher=_RaisingSQLitePublisher(),
            sleep_payload_builder=lambda *a, **kw: {},
        )
        with self.assertRaises(SQLiteAppError) as ctx:
            run_pipeline(config, self.target_date, deps, logging.getLogger("t"))
        self.assertEqual(ctx.exception.exit_code, 9)
        self.assertIn("SQLite", ctx.exception.title)
        self.assertIn("disk full", str(ctx.exception))

    def test_stdout_publish_error_wrapped(self):
        from ihealth.errors import StdoutAppError
        from ihealth.publishers.stdout import StdoutPublishError
        from ihealth.workflow import PipelineDeps, run_pipeline

        class _RaisingStdoutPublisher:
            def publish(self, health_data):
                raise StdoutPublishError("disk full")

        config = Config(
            notion_secret="t",
            database_id="db",
            health_export_dir=self.health_dir,
            archive_root=self.archive_root,
            birth_date=date(1990, 1, 1),
            log_level="info",
        )
        fetch_result = FetchResult(
            target_date=self.target_date,
            archive_dir=self.archive_root / self.target_date.isoformat(),
            archived={}, missing=[],
        )
        deps = PipelineDeps(
            fetcher=lambda **kwargs: fetch_result,
            parser=lambda *a, **kw: DailyHealthData(date=self.target_date, step_count=100),
            publisher=_RaisingStdoutPublisher(),
            sleep_payload_builder=lambda *a, **kw: {},
        )
        with self.assertRaises(StdoutAppError) as ctx:
            run_pipeline(config, self.target_date, deps, logging.getLogger("t"))
        self.assertEqual(ctx.exception.exit_code, 8)
        self.assertIn("stdout", ctx.exception.title)
        self.assertIn("disk full", str(ctx.exception))

    def test_markdown_publish_error_wrapped(self):
        from ihealth.errors import MarkdownAppError
        from ihealth.publishers.markdown import MarkdownPublishError
        from ihealth.workflow import PipelineDeps, run_pipeline

        class _RaisingMarkdownPublisher:
            def publish(self, health_data):
                raise MarkdownPublishError("disk full")

        config = Config(
            notion_secret="t",
            database_id="db",
            health_export_dir=self.health_dir,
            archive_root=self.archive_root,
            birth_date=date(1990, 1, 1),
            log_level="info",
        )

        fetch_result = FetchResult(
            target_date=self.target_date,
            archive_dir=self.archive_root / self.target_date.isoformat(),
            archived={},
            missing=[],
        )
        deps = PipelineDeps(
            fetcher=lambda **kwargs: fetch_result,
            parser=lambda *a, **kw: DailyHealthData(date=self.target_date, step_count=100),
            publisher=_RaisingMarkdownPublisher(),
            sleep_payload_builder=lambda *a, **kw: {},
        )
        with self.assertRaises(MarkdownAppError) as ctx:
            run_pipeline(config, self.target_date, deps, logging.getLogger("t"))
        self.assertEqual(ctx.exception.exit_code, 7)
        self.assertIn("Markdown", ctx.exception.title)
        self.assertIn("disk full", str(ctx.exception))


class TestPublisherReexports(unittest.TestCase):
    """codex round 1 PR #25 nice-to-have: refactor 後の workflow.py re-export を CI で固定.

    将来の cleanup で誤って re-export を落としたら検知できるよう、
    旧 import 経路と新 import 経路の **同一性** を assertIs で固定する.
    """

    def test_publisher_protocol_is_reexported(self):
        from ihealth import workflow
        from ihealth.publishers.base import Publisher
        self.assertIs(workflow.Publisher, Publisher)

    def test_dryrun_publisher_is_reexported(self):
        from ihealth import workflow
        from ihealth.publishers.dryrun import DryRunPublisher
        self.assertIs(workflow.DryRunPublisher, DryRunPublisher)

    def test_notion_publisher_is_reexported(self):
        from ihealth import workflow
        from ihealth.publishers.notion import NotionPublisher
        self.assertIs(workflow.NotionPublisher, NotionPublisher)

    def test_notion_helpers_are_reexported(self):
        from ihealth import workflow
        from ihealth.publishers.notion import (
            PROPERTY_MAP,
            NotionClient,
            NotionError,
            build_properties,
        )
        self.assertIs(workflow.PROPERTY_MAP, PROPERTY_MAP)
        self.assertIs(workflow.NotionClient, NotionClient)
        self.assertIs(workflow.NotionError, NotionError)
        self.assertIs(workflow.build_properties, build_properties)

    def test_notion_app_error_is_reexported(self):
        from ihealth import workflow
        from ihealth.errors import NotionAppError
        self.assertIs(workflow.NotionAppError, NotionAppError)


if __name__ == "__main__":
    unittest.main()
