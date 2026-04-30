"""Stdout Publisher (Phase 1 A3-stdout) の回帰テスト.

stdout に書く I/O は ``io.StringIO`` で捕捉し、実 stdout を汚さない.
"""

from __future__ import annotations

import io
import json
import logging
import os as _os
import subprocess
import sys as _sys
import tempfile
import textwrap
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from ihealth.models import DailyHealthData
from ihealth.publishers.stdout import (
    StdoutPublishError,
    StdoutPublisher,
)


def _logger() -> logging.Logger:
    return logging.getLogger("test-stdout-publisher")


class TestStdoutPublisherBasics(unittest.TestCase):
    def setUp(self):
        self.target_date = date(2026, 4, 22)
        self.stream = io.StringIO()
        self.log = MagicMock()
        self.pub = StdoutPublisher(
            target_date=self.target_date,
            logger=self.log,
            output_stream=self.stream,
        )

    def test_writes_single_json_line(self):
        data = DailyHealthData(date=self.target_date, step_count=100)
        self.pub.publish(data)
        output = self.stream.getvalue()
        # 末尾に改行 1 つ、改行は 1 個だけ (= 1 行)
        self.assertTrue(output.endswith("\n"))
        self.assertEqual(output.count("\n"), 1)

    def test_payload_is_valid_json(self):
        data = DailyHealthData(date=self.target_date, step_count=12345)
        self.pub.publish(data)
        output = self.stream.getvalue()
        parsed = json.loads(output)
        self.assertEqual(parsed, {"date": "2026-04-22", "step_count": 12345})

    def test_none_fields_omitted(self):
        data = DailyHealthData(date=self.target_date, step_count=100)
        self.pub.publish(data)
        parsed = json.loads(self.stream.getvalue())
        self.assertEqual(set(parsed.keys()), {"date", "step_count"})
        self.assertNotIn("distance_km", parsed)
        self.assertNotIn("sleep_hours", parsed)

    def test_multiple_fields(self):
        data = DailyHealthData(
            date=self.target_date,
            step_count=12345,
            distance_km=5.234,
            sleep_hours=7.5,
            body_mass_kg=65.5,
        )
        self.pub.publish(data)
        parsed = json.loads(self.stream.getvalue())
        self.assertEqual(parsed["step_count"], 12345)
        self.assertEqual(parsed["distance_km"], 5.23)  # round 2
        self.assertEqual(parsed["sleep_hours"], 7.5)
        self.assertEqual(parsed["body_mass_kg"], 65.5)

    def test_compact_separators(self):
        # JSON Lines では空白を抑える (parse 高速 / pipe 効率)
        data = DailyHealthData(
            date=self.target_date, step_count=100, distance_km=5.0,
        )
        self.pub.publish(data)
        output = self.stream.getvalue()
        # ``", "`` でなく ``","`` が separator (空白なし)
        self.assertIn('"step_count":100', output)
        self.assertNotIn(": ", output)

    def test_target_date_mismatch_raises(self):
        wrong = DailyHealthData(date=date(2026, 4, 23), step_count=1)
        with self.assertRaises(StdoutPublishError) as ctx:
            self.pub.publish(wrong)
        self.assertIn("target_date 不整合", str(ctx.exception))

    def test_invalid_value_raises(self):
        # parser bug で step_count=True が来たら fail-fast
        data = DailyHealthData.__new__(DailyHealthData)
        object.__setattr__(data, "date", self.target_date)
        from ihealth.publishers._payload import FIELD_SPECS
        for f in FIELD_SPECS:
            object.__setattr__(data, f, None)
        object.__setattr__(data, "step_count", True)
        with self.assertRaises(StdoutPublishError) as ctx:
            self.pub.publish(data)
        self.assertIn("step_count", str(ctx.exception))

    def test_logs_success(self):
        data = DailyHealthData(date=self.target_date, step_count=100)
        self.pub.publish(data)
        self.assertTrue(self.log.info.called)


class TestStdoutPublisherStream(unittest.TestCase):
    """``output_stream`` 注入経路の検証."""

    def test_default_stream_is_sys_stdout(self):
        # output_stream=None で sys.stdout が使われる
        target = date(2026, 4, 22)
        pub = StdoutPublisher(
            target_date=target, logger=MagicMock(), output_stream=None,
        )
        self.assertIsNone(pub._stream)  # 内部状態確認

    def test_broken_pipe_silent(self):
        # publish 中に BrokenPipeError が起きても raise しない (Unix 慣習)
        target = date(2026, 4, 22)
        pub = StdoutPublisher(target_date=target, logger=MagicMock())

        class _BrokenPipe:
            def write(self, s):
                raise BrokenPipeError("downstream closed")

            def flush(self):
                pass  # write で先に raise されるので呼ばれない

        pub._stream = _BrokenPipe()
        # raise しないこと (silent return)
        pub.publish(DailyHealthData(date=target, step_count=1))

    def test_oserror_during_write_raises_publish_error(self):
        target = date(2026, 4, 22)
        pub = StdoutPublisher(target_date=target, logger=MagicMock())

        class _BrokenStream:
            def write(self, s):
                raise OSError("disk full")

            def flush(self):
                pass

        pub._stream = _BrokenStream()
        with self.assertRaises(StdoutPublishError) as ctx:
            pub.publish(DailyHealthData(date=target, step_count=1))
        self.assertIn("disk full", str(ctx.exception))

    def test_oserror_during_flush_raises(self):
        target = date(2026, 4, 22)
        pub = StdoutPublisher(target_date=target, logger=MagicMock())

        class _FlushFails:
            def __init__(self):
                self.buffer = ""

            def write(self, s):
                self.buffer += s

            def flush(self):
                raise OSError("flush fail")

        pub._stream = _FlushFails()
        with self.assertRaises(StdoutPublishError) as ctx:
            pub.publish(DailyHealthData(date=target, step_count=1))
        self.assertIn("flush fail", str(ctx.exception))


class TestStdoutPublisherClosedStream(unittest.TestCase):
    """codex round 1 (PR #22) 指摘: closed stream の ValueError も wrap."""

    def test_closed_stringio_wrapped_to_publish_error(self):
        target = date(2026, 4, 22)
        s = io.StringIO()
        s.close()
        pub = StdoutPublisher(
            target_date=target, logger=MagicMock(), output_stream=s,
        )
        with self.assertRaises(StdoutPublishError):
            pub.publish(DailyHealthData(date=target, step_count=1))

    def test_unicode_error_wrapped(self):
        target = date(2026, 4, 22)

        class _AsciiOnlyStream:
            def write(self, s):
                raise UnicodeEncodeError("ascii", s, 0, 1, "bad")

            def flush(self):
                pass

        pub = StdoutPublisher(
            target_date=target, logger=MagicMock(),
            output_stream=_AsciiOnlyStream(),
        )
        with self.assertRaises(StdoutPublishError):
            pub.publish(DailyHealthData(date=target, step_count=1))


class TestStdoutBrokenPipeSubprocess(unittest.TestCase):
    """codex round 1 (PR #22) 指摘: 実 sys.stdout で BrokenPipe → 終了時 flush
    で "Exception ignored" が stderr に出ない (= 真に silent) 検証.

    subprocess で ``python -m ihealth_stdout_pub_smoke | true`` 形式を実行し,
    親プロセスから返った exit code と stderr を assert する.
    """

    @unittest.skipIf(_sys.platform == "win32", "POSIX pipe 限定")
    def test_broken_pipe_no_stderr_noise(self):
        # 直接 publisher を呼ぶ最小スクリプトを書き出して subprocess 実行
        src_path = Path(__file__).resolve().parent.parent / "src"
        script = textwrap.dedent("""
            import logging, sys
            from datetime import date
            from ihealth.models import DailyHealthData
            from ihealth.publishers.stdout import StdoutPublisher
            # 大きい payload を吐いて pipe を確実に詰まらせる
            log = logging.getLogger("smoke")
            target = date(2026, 4, 22)
            pub = StdoutPublisher(target_date=target, logger=log)
            for _ in range(100):
                pub.publish(DailyHealthData(date=target, step_count=1))
            sys.exit(0)
        """).strip()
        env = _os.environ.copy()
        env["PYTHONPATH"] = (
            f"{src_path}{_os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(_os.pathsep)
        )
        # python -c "<script>" | /usr/bin/true で head 役として true に
        producer = subprocess.Popen(
            [_sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        # CI portability のため /usr/bin/true ではなく sys.executable を使う
        # (codex round 2 指摘 2026-04-29 PR#22). 受信側が即時 close するだけ
        # でよいので最小スクリプト.
        consumer = subprocess.Popen(
            [_sys.executable, "-c", "import sys; sys.exit(0)"],
            stdin=producer.stdout,
        )
        # producer の stdout を Popen がコピー保持しないよう close
        if producer.stdout is not None:
            producer.stdout.close()
        consumer.wait(timeout=30)
        try:
            producer_stderr_bytes = (
                producer.stderr.read() if producer.stderr is not None else b""
            )
        finally:
            if producer.stderr is not None:
                producer.stderr.close()
        producer_stderr = producer_stderr_bytes.decode("utf-8", errors="replace")
        producer.wait(timeout=30)

        # producer は exit 0 (BrokenPipe を silent に処理)
        self.assertEqual(
            producer.returncode, 0,
            f"producer exit={producer.returncode}, stderr={producer_stderr!r}",
        )
        # stderr に "Exception ignored" が出ない (= 真に silent)
        self.assertNotIn("Exception ignored", producer_stderr)
        self.assertNotIn("BrokenPipeError", producer_stderr)


class TestStdoutSatisfiesProtocol(unittest.TestCase):
    def test_publish_callable(self):
        from ihealth.workflow import Publisher
        pub = StdoutPublisher(
            target_date=date(2026, 4, 22), logger=_logger(),
            output_stream=io.StringIO(),
        )
        self.assertTrue(callable(pub.publish))
        # Protocol そのものは structural なので isinstance 不可
        self.assertTrue(hasattr(Publisher, "__name__"))


if __name__ == "__main__":
    unittest.main()
