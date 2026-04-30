"""notifier.py の回帰テスト (subprocess.run のモック)。

``notify_failure`` が:

* ``osascript -e <script>`` の形式で subprocess.run を呼ぶ
* AppleScript リテラル内で ダブルクオート / バックスラッシュ / 改行を正しくエスケープ
* osascript 不在 (非 macOS) でも例外を投げない
* subprocess の失敗 (TimeoutExpired / CalledProcessError / OSError) を握り潰す

を検証する。個別ヘルパー (_escape_applescript_string / _build_applescript) も単体で
エッジケースをカバーする。
"""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from ihealth.notifier import (
    _build_applescript,
    _escape_applescript_string,
    notify_failure,
)


class TestEscapeApplescriptString(unittest.TestCase):
    def test_plain_text_unchanged(self):
        self.assertEqual(_escape_applescript_string("hello"), "hello")

    def test_japanese_unchanged(self):
        # 日本語は UTF-8 でそのまま通る
        self.assertEqual(_escape_applescript_string("日本語"), "日本語")

    def test_double_quote_escaped(self):
        self.assertEqual(_escape_applescript_string('he said "hi"'), 'he said \\"hi\\"')

    def test_backslash_first_then_quote(self):
        # バックスラッシュを先にエスケープしないと、後続の " のエスケープが二重置換される
        self.assertEqual(_escape_applescript_string('a\\"b'), 'a\\\\\\"b')

    def test_newline_escaped(self):
        # AppleScript リテラル内に生の改行が入るとパーサエラー → \n で保持
        self.assertEqual(_escape_applescript_string("line1\nline2"), "line1\\nline2")

    def test_carriage_return_escaped(self):
        self.assertEqual(_escape_applescript_string("a\rb"), "a\\rb")

    def test_injection_attempt_is_neutralized(self):
        # 悪意入力: "} で AppleScript 構造を抜けようとする
        malicious = '"}, default answer:"pwned'
        escaped = _escape_applescript_string(malicious)
        # すべての " が \" になっている (先行 \ のない生の " は 1 つも残らない)
        import re
        unescaped_quotes = re.findall(r'(?<!\\)"', escaped)
        self.assertEqual(unescaped_quotes, [], f"生の \" が残っている: {escaped!r}")
        # 期待形式そのものも直接確認
        self.assertEqual(escaped, r'\"}, default answer:\"pwned')


class TestBuildApplescript(unittest.TestCase):
    def test_uses_reminders_tell_block(self):
        s = _build_applescript("タイトル", "本文")
        self.assertIn('tell application "Reminders"', s)
        self.assertIn("end tell", s)

    def test_embeds_name_and_body(self):
        s = _build_applescript("title", "body")
        self.assertIn('name:"title"', s)
        self.assertIn('body:"body"', s)

    def test_escapes_embedded_quotes(self):
        s = _build_applescript('t"itle', "b")
        # title の " がエスケープされて、AppleScript の name:"..." の閉じには
        # ならない。つまり name:"t\"itle" の形
        self.assertIn('name:"t\\"itle"', s)


class TestNotifyFailureSuccess(unittest.TestCase):
    def test_calls_osascript_with_e_flag(self):
        with patch("ihealth.notifier.os.path.exists", return_value=True), \
             patch("ihealth.notifier.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b"",
            )
            notify_failure("タイトル", "本文")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "/usr/bin/osascript")
        self.assertEqual(cmd[1], "-e")
        script = cmd[2]
        self.assertIn("タイトル", script)
        self.assertIn("本文", script)

    def test_timeout_is_set(self):
        with patch("ihealth.notifier.os.path.exists", return_value=True), \
             patch("ihealth.notifier.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b"",
            )
            notify_failure("t", "b")
        kwargs = mock_run.call_args.kwargs
        self.assertIn("timeout", kwargs)
        self.assertGreater(kwargs["timeout"], 0)

    def test_capture_output_true(self):
        with patch("ihealth.notifier.os.path.exists", return_value=True), \
             patch("ihealth.notifier.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b"",
            )
            notify_failure("t", "b")
        kwargs = mock_run.call_args.kwargs
        # capture_output=True か、stdout/stderr=PIPE のいずれか
        self.assertTrue(
            kwargs.get("capture_output")
            or (kwargs.get("stdout") is subprocess.PIPE and kwargs.get("stderr") is subprocess.PIPE),
            "capture_output=True (もしくは stdout/stderr=PIPE) が必要",
        )


class TestNotifyFailureGracefulErrors(unittest.TestCase):
    """通知失敗で本体処理が止まらない = 例外が漏れない契約の検証"""

    def test_osascript_not_found_silently_skipped(self):
        # Linux / 権限未付与環境で /usr/bin/osascript が存在しないケース。
        # 例外を出さず return するだけでなく、**subprocess.run を呼ばずに早期 return**
        # することを確認 (将来 regress で exists チェックが抜けても
        # subprocess.run が呼ばれていれば検知できる)。
        with patch("ihealth.notifier.os.path.exists", return_value=False), \
             patch("ihealth.notifier.subprocess.run") as mock_run:
            notify_failure("t", "b")
            self.assertEqual(
                mock_run.call_count, 0,
                "osascript 不在時は subprocess.run を呼ばない契約",
            )

    def test_calledprocesserror_suppressed(self):
        with patch("ihealth.notifier.os.path.exists", return_value=True), \
             patch("ihealth.notifier.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1,
                cmd=["/usr/bin/osascript", "-e", "x"],
                stderr=b"permission denied",
            )
            # 例外が漏れない
            notify_failure("t", "b")

    def test_timeout_suppressed(self):
        with patch("ihealth.notifier.os.path.exists", return_value=True), \
             patch("ihealth.notifier.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["/usr/bin/osascript"], timeout=10,
            )
            notify_failure("t", "b")

    def test_oserror_suppressed(self):
        with patch("ihealth.notifier.os.path.exists", return_value=True), \
             patch("ihealth.notifier.subprocess.run") as mock_run:
            mock_run.side_effect = OSError(13, "Permission denied")
            notify_failure("t", "b")


if __name__ == "__main__":
    unittest.main()
