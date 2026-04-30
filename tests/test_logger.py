"""logger.configure の引数化 + LOG_LEVEL 許可値検証のテスト (Issue #17)。"""

from __future__ import annotations

import io
import logging
import tempfile
import unittest
from pathlib import Path

from ihealth.logger import ALLOWED_LEVELS, configure


class TestConfigureArgs(unittest.TestCase):
    def tearDown(self):
        # 他テストへの副作用 (ihealth ロガーに handler が残る) を避けるため cleanup
        log = logging.getLogger("ihealth")
        for h in list(log.handlers):
            log.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass

    def test_stream_injection(self):
        stream = io.StringIO()
        log = configure("info", stream=stream, enable_file_handler=False)
        log.info("hello")
        # logging.disable(INFO) が tests/__init__.py で効いているので、
        # INFO は stream に書かれない場合があるが、WARNING は書かれる
        log.warning("world")
        self.assertIn("world", stream.getvalue())

    def test_log_file_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "sub" / "test.log"
            stream = io.StringIO()  # stream も明示 (stderr への漏洩を防ぐ)
            log = configure(
                "warning",
                log_file=log_file,
                stream=stream,
                enable_file_handler=True,
            )
            log.warning("to file")
            # 親ディレクトリも作成される
            self.assertTrue(log_file.parent.is_dir())
            self.assertTrue(log_file.exists())

    def test_enable_file_handler_false_creates_no_fd(self):
        # enable_file_handler=False ならファイル handler が付かない
        stream = io.StringIO()
        log = configure("info", enable_file_handler=False, stream=stream)
        handlers = logging.getLogger("ihealth").handlers
        # StreamHandler のみ (TimedRotatingFileHandler は無い)
        self.assertEqual(len(handlers), 1)
        self.assertIsInstance(handlers[0], logging.StreamHandler)
        self.assertNotIsInstance(handlers[0], logging.FileHandler)

    def test_invalid_level_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            configure("degub")  # typo
        self.assertIn("LOG_LEVEL", str(ctx.exception))

    def test_level_case_insensitive(self):
        # 大文字でも許容される (既存の設定ファイルが DEBUG / Info などと混在しても壊れない)
        configure("DEBUG", enable_file_handler=False, stream=io.StringIO())
        configure("Info", enable_file_handler=False, stream=io.StringIO())
        configure("WARNING", enable_file_handler=False, stream=io.StringIO())
        configure("  error  ", enable_file_handler=False, stream=io.StringIO())

    def test_allowed_levels_set(self):
        self.assertEqual(
            ALLOWED_LEVELS,
            frozenset({"debug", "info", "warning", "error"}),
        )

    def test_idempotent_no_fd_leak(self):
        # 複数回 configure を呼んでも handler が増殖せず既存 FD は close される
        stream = io.StringIO()
        for _ in range(5):
            configure("info", enable_file_handler=False, stream=stream)
        handlers = logging.getLogger("ihealth").handlers
        self.assertEqual(len(handlers), 1, "configure が冪等であるべき")


class TestConfigLogLevelValidation(unittest.TestCase):
    """config.load_config 側の LOG_LEVEL 許可値検証 (Issue #17)"""

    def test_invalid_log_level_in_env_raises_config_error(self):
        from ihealth.config import ConfigError, load_config

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", delete=False, encoding="utf-8"
        ) as f:
            f.write(
                "NOTION_SECRET=dummy\n"
                "DATABASE_ID=dummy\n"
                "HEALTH_EXPORT_DIR=/tmp\n"
                "BIRTH_DATE=1990-01-01\n"
                "LOG_LEVEL=degub\n"   # typo
            )
            tmp_path = Path(f.name)
        try:
            with self.assertRaises(ConfigError) as ctx:
                load_config(tmp_path)
            self.assertIn("LOG_LEVEL", str(ctx.exception))
        finally:
            tmp_path.unlink()


if __name__ == "__main__":
    unittest.main()
