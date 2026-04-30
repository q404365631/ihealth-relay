"""Slack Publisher (Phase 1 A3-slack) の回帰テスト.

実 Slack に POST しないよう ``urllib.request.urlopen`` をモックする.
"""

from __future__ import annotations

import io
import json
import logging
import unittest
import urllib.error
from datetime import date
from unittest.mock import MagicMock, patch

from ihealth.models import DailyHealthData
from ihealth.publishers.slack import (
    ALLOWED_WEBHOOK_HOSTS,
    SlackPublishError,
    SlackPublisher,
    _mask_webhook_url,
    _redact_secret,
    render_message,
    validate_webhook_url,
)


_FAKE_WEBHOOK = "https://hooks.slack.com/services/T0/B0/SECRET_TOKEN"


def _logger() -> logging.Logger:
    return logging.getLogger("test-slack-publisher")


def _ok_response() -> MagicMock:
    """``ok`` を返す urllopen モック (Slack の 200 応答)."""
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    fake.status = 200
    fake.read = MagicMock(return_value=b"ok")
    return fake


class TestMaskWebhookURL(unittest.TestCase):
    def test_extracts_host(self):
        self.assertEqual(
            _mask_webhook_url("https://hooks.slack.com/services/T0/B0/secret"),
            "hooks.slack.com",
        )

    def test_invalid_url_returns_placeholder(self):
        self.assertEqual(_mask_webhook_url(""), "(unknown host)")

    def test_long_host_truncated(self):
        long = "https://" + "x" * 100 + "/path"
        masked = _mask_webhook_url(long)
        self.assertTrue(masked.endswith("..."))
        self.assertLess(len(masked), 60)

    def test_userinfo_form_returns_real_host(self):
        # codex round 1 PR #24 指摘: parsed.netloc を使うと
        # "secret@hooks.slack.com" が露出していたバグ. parsed.hostname で修正.
        masked = _mask_webhook_url("https://SECRET@hooks.slack.com/services/x")
        self.assertEqual(masked, "hooks.slack.com")
        self.assertNotIn("SECRET", masked)


class TestValidateWebhookURL(unittest.TestCase):
    """codex round 1 PR #24 指摘: SSRF / 任意 host 拒否."""

    _GOOD = "https://hooks.slack.com/services/T0/B0/SECRET"

    def test_good_passthrough(self):
        self.assertEqual(validate_webhook_url(self._GOOD, source_label="x"), self._GOOD)

    def test_govslack_allowed(self):
        url = "https://hooks.slack-gov.com/services/T0/B0/SECRET"
        self.assertEqual(validate_webhook_url(url, source_label="x"), url)

    def test_arbitrary_host_rejected(self):
        # SSRF / 外送防止: hooks.slack.com 以外は reject
        with self.assertRaises(ValueError) as ctx:
            validate_webhook_url(
                "https://evil.example.com/services/T0/B0/SECRET",
                source_label="--slack-webhook",
            )
        self.assertIn("host", str(ctx.exception).lower())

    def test_http_rejected(self):
        with self.assertRaises(ValueError):
            validate_webhook_url(
                "http://hooks.slack.com/services/x/y/z", source_label="x",
            )

    def test_userinfo_rejected(self):
        with self.assertRaises(ValueError):
            validate_webhook_url(
                "https://user:pass@hooks.slack.com/services/x/y/z",
                source_label="x",
            )

    def test_userinfo_only_username_rejected(self):
        with self.assertRaises(ValueError):
            validate_webhook_url(
                "https://user@hooks.slack.com/services/x/y/z",
                source_label="x",
            )

    def test_path_outside_services_rejected(self):
        with self.assertRaises(ValueError):
            validate_webhook_url(
                "https://hooks.slack.com/api/incoming/x", source_label="x",
            )

    def test_query_rejected(self):
        with self.assertRaises(ValueError):
            validate_webhook_url(
                "https://hooks.slack.com/services/x/y/z?token=secret",
                source_label="x",
            )

    def test_fragment_rejected(self):
        with self.assertRaises(ValueError):
            validate_webhook_url(
                "https://hooks.slack.com/services/x/y/z#frag",
                source_label="x",
            )

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            validate_webhook_url("", source_label="x")

    def test_error_message_contains_source_label_not_secret(self):
        try:
            validate_webhook_url(
                "https://evil.example.com/services/T0/B0/SECRET",
                source_label="--slack-webhook",
            )
        except ValueError as exc:
            self.assertIn("--slack-webhook", str(exc))
            self.assertNotIn("SECRET", str(exc))

    def test_empty_userinfo_rejected(self):
        # codex round 2 PR #24: parsed.username == "" は falsy なので "@" の
        # 直接チェックが必要.
        with self.assertRaises(ValueError):
            validate_webhook_url(
                "https://@hooks.slack.com/services/T/B/SECRET", source_label="x",
            )

    def test_non_443_port_rejected(self):
        with self.assertRaises(ValueError):
            validate_webhook_url(
                "https://hooks.slack.com:444/services/T/B/SECRET", source_label="x",
            )

    def test_explicit_443_accepted(self):
        url = "https://hooks.slack.com:443/services/T/B/SECRET"
        self.assertEqual(validate_webhook_url(url, source_label="x"), url)

    def test_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            validate_webhook_url(
                "https://hooks.slack.com/services/../T/B/SECRET", source_label="x",
            )

    def test_url_encoded_path_rejected(self):
        with self.assertRaises(ValueError):
            validate_webhook_url(
                "https://hooks.slack.com/services/%2e%2e/api/SECRET",
                source_label="x",
            )

    def test_extra_path_segment_rejected(self):
        with self.assertRaises(ValueError):
            validate_webhook_url(
                "https://hooks.slack.com/services/T/B/SECRET/extra",
                source_label="x",
            )

    def test_short_path_rejected(self):
        with self.assertRaises(ValueError):
            validate_webhook_url(
                "https://hooks.slack.com/services/T/B", source_label="x",
            )

    def test_empty_segment_rejected(self):
        with self.assertRaises(ValueError):
            validate_webhook_url(
                "https://hooks.slack.com/services/T//SECRET", source_label="x",
            )

    def test_invalid_port_string_rejected(self):
        # codex round 3 PR #24 nice-to-have: stdlib ValueError を rewrap
        with self.assertRaises(ValueError) as ctx:
            validate_webhook_url(
                "https://hooks.slack.com:abc/services/T/B/SECRET",
                source_label="--slack-webhook",
            )
        self.assertIn("--slack-webhook", str(ctx.exception))

    def test_out_of_range_port_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            validate_webhook_url(
                "https://hooks.slack.com:99999/services/T/B/SECRET",
                source_label="--slack-webhook",
            )
        self.assertIn("--slack-webhook", str(ctx.exception))

    def test_unicode_in_path_rejected(self):
        # codex round 3 PR #24 nice-to-have: literal Unicode token を reject
        with self.assertRaises(ValueError) as ctx:
            validate_webhook_url(
                "https://hooks.slack.com/services/T/B/ＳＥＣＲＥＴ",
                source_label="--slack-webhook",
            )
        self.assertIn("non-ASCII", str(ctx.exception))

    def test_whitespace_in_segment_rejected(self):
        with self.assertRaises(ValueError):
            validate_webhook_url(
                "https://hooks.slack.com/services/T/B /SECRET",
                source_label="--slack-webhook",
            )


class TestRedactSecret(unittest.TestCase):
    _URL = "https://hooks.slack.com/services/T0XXXX/B0XXXX/SECRET_TOKEN_LONG"

    def test_substitutes_full_url(self):
        msg = f"connection refused: {self._URL}"
        out = _redact_secret(msg, self._URL, "hooks.slack.com")
        self.assertNotIn("SECRET_TOKEN_LONG", out)
        self.assertIn("https://hooks.slack.com/...", out)

    def test_no_url_in_text_unchanged(self):
        out = _redact_secret("dns failure", self._URL, "hooks.slack.com")
        self.assertEqual(out, "dns failure")

    def test_empty_url_passthrough(self):
        self.assertEqual(_redact_secret("hello", "", "host"), "hello")

    def test_handles_non_string_text(self):
        err = OSError(2, "No such file")
        out = _redact_secret(err, self._URL, "hooks.slack.com")
        self.assertIn("No such file", out)

    def test_lowercase_url_redacted(self):
        # codex round 2 PR #24: case-insensitive 配慮 (URL 自体に大文字 token
        # が混じっても、別箇所で lower 化されたコピーを redact する)
        msg = f"see {self._URL.lower()} for details"
        out = _redact_secret(msg, self._URL, "hooks.slack.com")
        self.assertNotIn("secret_token_long", out.lower())

    def test_path_only_redacted(self):
        # 例外メッセージが "/services/T0/B0/SECRET" だけ含むケース
        msg = "Server log: hit /services/T0XXXX/B0XXXX/SECRET_TOKEN_LONG"
        out = _redact_secret(msg, self._URL, "hooks.slack.com")
        self.assertNotIn("SECRET_TOKEN_LONG", out)

    def test_url_encoded_redacted(self):
        # URL-encoded 形式 (例: HTTP error body に encoded URL が含まれる)
        import urllib.parse as _up
        encoded = _up.quote(self._URL, safe="")
        msg = f"redirect to {encoded}"
        out = _redact_secret(msg, self._URL, "hooks.slack.com")
        self.assertNotIn("SECRET_TOKEN_LONG", out)

    def test_token_alone_redacted(self):
        # secret token 単体がメッセージに混ざるケース
        msg = "auth failed: token=SECRET_TOKEN_LONG expired"
        out = _redact_secret(msg, self._URL, "hooks.slack.com")
        self.assertNotIn("SECRET_TOKEN_LONG", out)

    def test_lowercase_path_only_redacted(self):
        # codex round 3 PR #24 指摘: lowercase された path-only でも漏れない
        msg = "Server log: hit /services/t0xxxx/b0xxxx/secret_token_long"
        out = _redact_secret(msg, self._URL, "hooks.slack.com")
        self.assertNotIn("secret_token_long", out)

    def test_lowercase_token_only_redacted(self):
        msg = "auth: lowercased=secret_token_long"
        out = _redact_secret(msg, self._URL, "hooks.slack.com")
        self.assertNotIn("secret_token_long", out)

    def test_mixed_case_token_redacted(self):
        # codex round 4 PR #24 指摘: re.IGNORECASE で mixed-case 完全吸収
        msg = "auth=SeCrEt_ToKeN_LoNg"
        out = _redact_secret(msg, self._URL, "hooks.slack.com")
        self.assertNotIn("SeCrEt_ToKeN_LoNg", out)

    def test_mixed_case_path_redacted(self):
        msg = "Server log: /Services/T0XXXX/B0XXXX/SeCrEt_ToKeN_LoNg"
        out = _redact_secret(msg, self._URL, "hooks.slack.com")
        self.assertNotIn("SeCrEt_ToKeN_LoNg", out)

    def test_tail_token_with_leading_slash_redacted(self):
        # codex round 5 PR #24 指摘: tail token boundary regex から ``/`` を
        # 除去して `/SECRET_TOKEN_LONG` 形式も redact する
        msg = "suffix /SECRET_TOKEN_LONG"
        out = _redact_secret(msg, self._URL, "hooks.slack.com")
        self.assertNotIn("SECRET_TOKEN_LONG", out)

    def test_tail_token_with_trailing_slash_redacted(self):
        msg = "suffix SECRET_TOKEN_LONG/extra"
        out = _redact_secret(msg, self._URL, "hooks.slack.com")
        self.assertNotIn("SECRET_TOKEN_LONG", out)

    def test_mixed_case_url_encoded_redacted(self):
        import urllib.parse as _up
        # URL を mixed-case で encode (例: %2F vs %2f)
        encoded = _up.quote(self._URL, safe="").upper()
        msg = f"redirect to {encoded}"
        out = _redact_secret(msg, self._URL, "hooks.slack.com")
        self.assertNotIn("SECRET_TOKEN_LONG", out.upper())

    def test_short_token_not_tail_redacted(self):
        # codex round 4 PR #24 nice-to-have: 短い token (< 8 文字) の bare 出現は
        # 誤検知が大きいので boundary 置換しない. URL/path 単位の置換は変わらず効く.
        url = "https://hooks.slack.com/services/T0/B0/short"
        msg = "saw token=short in body"
        out = _redact_secret(msg, url, "hooks.slack.com")
        # 短い token 単独は残る (= 偽陽性回避)
        self.assertIn("short", out)
        # しかし full URL や path は redact される
        self.assertNotIn(url, _redact_secret(f"err: {url}", url, "hooks.slack.com"))


class TestSlackPublisherSecretRedactInBodyOk(unittest.TestCase):
    """codex round 2 PR #24 指摘: 200+body!=ok 経路の body も redact."""

    def test_non_ok_body_redacted(self):
        target = date(2026, 4, 22)
        url = "https://hooks.slack.com/services/T0XXXX/B0XXXX/MY_SECRET_TOKEN"
        pub = SlackPublisher(
            webhook_url=url, target_date=target, logger=MagicMock(),
        )
        # response.read() が secret を含む body を返す (本来 Slack は token を
        # echo back しないが、誤設定 / proxy 等で混入の可能性)
        fake = MagicMock()
        fake.__enter__ = MagicMock(return_value=fake)
        fake.__exit__ = MagicMock(return_value=False)
        fake.status = 200
        fake.read = MagicMock(return_value=f"see {url} for details".encode("utf-8"))
        with patch("urllib.request.urlopen", return_value=fake):
            with self.assertRaises(SlackPublishError) as ctx:
                pub.publish(DailyHealthData(date=target, step_count=1))
        self.assertNotIn("MY_SECRET_TOKEN", str(ctx.exception))


class TestRenderMessage(unittest.TestCase):
    def test_only_date_shows_no_data(self):
        data = DailyHealthData(date=date(2026, 4, 22))
        text = render_message(data)
        self.assertIn("*2026-04-22 健康データ*", text)
        self.assertIn("計測データなし", text)

    def test_step_count_rendered(self):
        data = DailyHealthData(date=date(2026, 4, 22), step_count=12345)
        text = render_message(data)
        self.assertIn("• 歩数: 12345 歩", text)

    def test_distance_rounded(self):
        data = DailyHealthData(date=date(2026, 4, 22), distance_km=5.234)
        text = render_message(data)
        self.assertIn("• 移動距離: 5.23 km", text)

    def test_none_fields_omitted(self):
        data = DailyHealthData(date=date(2026, 4, 22), step_count=100)
        text = render_message(data)
        self.assertNotIn("移動距離", text)
        self.assertNotIn("睡眠時間", text)

    def test_invalid_value_raises(self):
        # parser bug: step_count=True
        data = DailyHealthData.__new__(DailyHealthData)
        object.__setattr__(data, "date", date(2026, 4, 22))
        from ihealth.publishers._payload import FIELD_SPECS
        for f in FIELD_SPECS:
            object.__setattr__(data, f, None)
        object.__setattr__(data, "step_count", True)
        with self.assertRaises(SlackPublishError):
            render_message(data)


class TestSlackPublisherBasics(unittest.TestCase):
    def setUp(self):
        self.target_date = date(2026, 4, 22)
        self.log = MagicMock()
        self.pub = SlackPublisher(
            webhook_url=_FAKE_WEBHOOK,
            target_date=self.target_date,
            logger=self.log,
        )

    def test_empty_webhook_rejected_at_construct(self):
        with self.assertRaises(ValueError):
            SlackPublisher(
                webhook_url="",
                target_date=self.target_date,
                logger=self.log,
            )

    def test_target_date_mismatch_raises(self):
        wrong = DailyHealthData(date=date(2026, 4, 23), step_count=1)
        with self.assertRaises(SlackPublishError):
            self.pub.publish(wrong)

    def test_happy_path_posts_json(self):
        data = DailyHealthData(date=self.target_date, step_count=12345)
        with patch("urllib.request.urlopen", return_value=_ok_response()) as mock_open:
            self.pub.publish(data)
        # POST だけ呼ばれた
        self.assertEqual(mock_open.call_count, 1)
        req = mock_open.call_args[0][0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, _FAKE_WEBHOOK)
        # body が JSON で {"text": ...} 形式
        body = json.loads(req.data.decode("utf-8"))
        self.assertIn("text", body)
        self.assertIn("12345", body["text"])

    def test_logs_success(self):
        with patch("urllib.request.urlopen", return_value=_ok_response()):
            self.pub.publish(DailyHealthData(date=self.target_date, step_count=1))
        self.assertTrue(self.log.info.called)

    def test_logs_do_not_contain_full_webhook_url(self):
        # secret token を含む URL がログに出ないこと
        with patch("urllib.request.urlopen", return_value=_ok_response()):
            self.pub.publish(DailyHealthData(date=self.target_date, step_count=1))
        for call in self.log.info.call_args_list:
            args = call[0]
            for arg in args:
                self.assertNotIn("SECRET_TOKEN", str(arg))


class TestSlackPublisherErrors(unittest.TestCase):
    def setUp(self):
        self.target_date = date(2026, 4, 22)
        self.log = MagicMock()
        self.pub = SlackPublisher(
            webhook_url=_FAKE_WEBHOOK,
            target_date=self.target_date,
            logger=self.log,
        )

    def test_http_500_raises(self):
        # urlopen が HTTPError を raise (status=500)
        err = urllib.error.HTTPError(
            url=_FAKE_WEBHOOK, code=500, msg="Internal Server Error",
            hdrs=None, fp=io.BytesIO(b"server died"),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(SlackPublishError) as ctx:
                self.pub.publish(DailyHealthData(date=self.target_date, step_count=1))
            msg = str(ctx.exception)
            self.assertIn("500", msg)
            self.assertIn("hooks.slack.com", msg)
            self.assertNotIn("SECRET_TOKEN", msg)

    def test_url_error_raises(self):
        err = urllib.error.URLError("connection refused")
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(SlackPublishError) as ctx:
                self.pub.publish(DailyHealthData(date=self.target_date, step_count=1))
            self.assertIn("接続エラー", str(ctx.exception))
            self.assertNotIn("SECRET_TOKEN", str(ctx.exception))

    def test_url_error_with_full_url_in_reason_redacted(self):
        # codex round 1 PR #24 指摘: reason に full URL が入るケース
        # (例: socket.gaierror が full URL を持つ).
        err = urllib.error.URLError(f"DNS lookup failed for {_FAKE_WEBHOOK}")
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(SlackPublishError) as ctx:
                self.pub.publish(DailyHealthData(date=self.target_date, step_count=1))
            msg = str(ctx.exception)
            self.assertNotIn("SECRET_TOKEN", msg)
            self.assertIn("hooks.slack.com", msg)

    def test_http_error_with_secret_in_body_redacted(self):
        err = urllib.error.HTTPError(
            url=_FAKE_WEBHOOK, code=500,
            msg="Server died at SECRET_TOKEN",
            hdrs=None,
            fp=io.BytesIO(f"see {_FAKE_WEBHOOK} for details".encode("utf-8")),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(SlackPublishError) as ctx:
                self.pub.publish(DailyHealthData(date=self.target_date, step_count=1))
            msg = str(ctx.exception)
            self.assertNotIn("SECRET_TOKEN", msg)

    def test_http_error_no_chained_cause(self):
        # codex round 1 PR #24: __cause__ から exc.url が露出しないよう
        # ``raise ... from None`` で chain 切断していること.
        err = urllib.error.HTTPError(
            url=_FAKE_WEBHOOK, code=500, msg="boom",
            hdrs=None, fp=io.BytesIO(b"boom"),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            try:
                self.pub.publish(DailyHealthData(date=self.target_date, step_count=1))
            except SlackPublishError as exc:
                self.assertIsNone(exc.__cause__)
                self.assertTrue(exc.__suppress_context__)

    def test_url_error_no_chained_cause(self):
        err = urllib.error.URLError("dns fail")
        with patch("urllib.request.urlopen", side_effect=err):
            try:
                self.pub.publish(DailyHealthData(date=self.target_date, step_count=1))
            except SlackPublishError as exc:
                self.assertIsNone(exc.__cause__)
                self.assertTrue(exc.__suppress_context__)

    def test_http_error_fp_closed(self):
        # codex round 1 PR #24 nice-to-have: HTTPError.fp は exc.close() で閉じる
        fp = io.BytesIO(b"body")
        err = urllib.error.HTTPError(
            url=_FAKE_WEBHOOK, code=500, msg="boom", hdrs=None, fp=fp,
        )
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(SlackPublishError):
                self.pub.publish(DailyHealthData(date=self.target_date, step_count=1))
        self.assertTrue(fp.closed, "HTTPError.fp が close されていない")

    def test_non_ok_response_raises(self):
        # 200 だが body が "ok" 以外 (= app uninstall 等)
        fake = MagicMock()
        fake.__enter__ = MagicMock(return_value=fake)
        fake.__exit__ = MagicMock(return_value=False)
        fake.status = 200
        fake.read = MagicMock(return_value=b"no_team")
        with patch("urllib.request.urlopen", return_value=fake):
            with self.assertRaises(SlackPublishError) as ctx:
                self.pub.publish(DailyHealthData(date=self.target_date, step_count=1))
            self.assertIn("ok", str(ctx.exception).lower())
            self.assertIn("no_team", str(ctx.exception))

    def test_invalid_value_raises_publish_error(self):
        # render_message が SlackPublishError を raise → そのまま伝播
        data = DailyHealthData.__new__(DailyHealthData)
        object.__setattr__(data, "date", self.target_date)
        from ihealth.publishers._payload import FIELD_SPECS
        for f in FIELD_SPECS:
            object.__setattr__(data, f, None)
        object.__setattr__(data, "step_count", True)
        with self.assertRaises(SlackPublishError):
            self.pub.publish(data)


class TestSlackSatisfiesProtocol(unittest.TestCase):
    def test_publish_callable(self):
        from ihealth.workflow import Publisher
        pub = SlackPublisher(
            webhook_url=_FAKE_WEBHOOK,
            target_date=date(2026, 4, 22),
            logger=_logger(),
        )
        self.assertTrue(callable(pub.publish))
        self.assertTrue(hasattr(Publisher, "__name__"))


if __name__ == "__main__":
    unittest.main()
