"""notion.py の回帰テスト (urllib.request.urlopen のモック)。

`NotionClient` は副作用 (HTTP 呼び出し) を持つので、モジュール境界で
``ihealth.publishers.notion.urllib.request.urlopen`` にパッチして契約を検証する:

* Request に載るメソッド / ヘッダ / body の構造
* 429 / 5xx / URLError のリトライ挙動
* 4xx 即失敗
* build_properties の変換仕様と DB schema 未存在プロパティの skip
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from datetime import date
from unittest.mock import MagicMock, patch

from ihealth.models import DailyHealthData
from ihealth.publishers.notion import (
    PROPERTY_MAP,
    NotionClient,
    NotionError,
    _parse_retry_after,
    _should_retry_http,
    build_properties,
)


def _make_resp(body: bytes) -> MagicMock:
    """urlopen が返す context manager モックを組み立てる。"""
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestNotionHeaders(unittest.TestCase):
    def test_authorization_header_is_bearer(self):
        c = NotionClient("secret-token", "db_id")
        body = json.dumps(
            {"properties": {"foo": {"type": "number"}}}
        ).encode("utf-8")
        with patch("ihealth.publishers.notion.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_resp(body)
            c.fetch_database_schema()
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_header("Authorization"), "Bearer secret-token")

    def test_notion_version_header_pinned(self):
        # 定数 API_VERSION が意図せず変わった時に気付けるよう、文字列を pin する
        # (実装側は依然 2022-06-28 API の shape (databases/{id}/query など) に
        # 依存しているので、version を上げる場合は endpoint / 応答 shape の
        # 再確認が必要 = テストもセットで見直す契約)。
        c = NotionClient("t", "db_id")
        body = json.dumps({"properties": {}}).encode("utf-8")
        with patch("ihealth.publishers.notion.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_resp(body)
            c.fetch_database_schema()
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_header("Notion-version"), "2022-06-28")
        # 念のため定数との同期も確認
        self.assertEqual(NotionClient.API_VERSION, "2022-06-28")

    def test_accept_json(self):
        c = NotionClient("t", "db_id")
        body = json.dumps({"properties": {}}).encode("utf-8")
        with patch("ihealth.publishers.notion.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_resp(body)
            c.fetch_database_schema()
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_header("Accept"), "application/json")


class TestFetchDatabaseSchema(unittest.TestCase):
    def test_returns_property_name_to_type_map(self):
        body = json.dumps({"properties": {
            "Step Count": {"type": "number"},
            "Date": {"type": "date"},
        }}).encode("utf-8")
        with patch("ihealth.publishers.notion.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_resp(body)
            c = NotionClient("t", "db_id")
            schema = c.fetch_database_schema()
        self.assertEqual(schema, {"Step Count": "number", "Date": "date"})
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_method(), "GET")
        self.assertIn("/databases/db_id", req.full_url)

    def test_raises_on_non_dict_properties(self):
        body = json.dumps({"properties": "invalid"}).encode("utf-8")
        with patch("ihealth.publishers.notion.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_resp(body)
            c = NotionClient("t", "db_id")
            with self.assertRaises(NotionError):
                c.fetch_database_schema()


class TestFindPageByDate(unittest.TestCase):
    def test_posts_date_filter(self):
        body = json.dumps({"results": [{"id": "abc123"}], "has_more": False}).encode("utf-8")
        with patch("ihealth.publishers.notion.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_resp(body)
            c = NotionClient("t", "db_id")
            page_id = c.find_page_by_date(date(2026, 4, 22))
        self.assertEqual(page_id, "abc123")
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_method(), "POST")
        self.assertIn("/databases/db_id/query", req.full_url)
        payload = json.loads(req.data)
        self.assertEqual(
            payload["filter"]["property"], NotionClient.DEFAULT_DATE_PROPERTY_NAME,
        )
        self.assertEqual(
            payload["filter"]["date"]["equals"], "2026-04-22",
        )

    def test_returns_none_when_no_results(self):
        body = json.dumps({"results": [], "has_more": False}).encode("utf-8")
        with patch("ihealth.publishers.notion.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_resp(body)
            c = NotionClient("t", "db_id")
            self.assertIsNone(c.find_page_by_date(date(2026, 4, 22)))

    def test_multiple_results_picks_first(self):
        body = json.dumps({
            "results": [{"id": "first"}, {"id": "second"}],
            "has_more": False,
        }).encode("utf-8")
        with patch("ihealth.publishers.notion.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_resp(body)
            c = NotionClient("t", "db_id")
            self.assertEqual(c.find_page_by_date(date(2026, 4, 22)), "first")

    def test_missing_has_more_raises(self):
        # Notion API 契約違反 (has_more キー欠落) は NotionError
        body = json.dumps({"results": []}).encode("utf-8")
        with patch("ihealth.publishers.notion.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_resp(body)
            c = NotionClient("t", "db_id")
            with self.assertRaises(NotionError):
                c.find_page_by_date(date(2026, 4, 22))


class TestUpdatePage(unittest.TestCase):
    def test_patch_with_properties_body(self):
        body = json.dumps({"object": "page"}).encode("utf-8")
        with patch("ihealth.publishers.notion.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_resp(body)
            c = NotionClient("t", "db_id")
            c.update_page("page123", {"Step Count": {"number": 12345}})
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_method(), "PATCH")
        self.assertIn("/pages/page123", req.full_url)
        payload = json.loads(req.data)
        self.assertEqual(payload, {"properties": {"Step Count": {"number": 12345}}})

    def test_empty_page_id_raises(self):
        c = NotionClient("t", "db_id")
        with self.assertRaises(NotionError):
            c.update_page("", {"foo": {"number": 1}})

    def test_non_dict_properties_raises(self):
        c = NotionClient("t", "db_id")
        with self.assertRaises(NotionError):
            c.update_page("page123", "not-a-dict")  # type: ignore[arg-type]


class TestRetryLogic(unittest.TestCase):
    def _make_http_error(self, code: int, retry_after: str = "") -> urllib.error.HTTPError:
        hdrs = {"Retry-After": retry_after} if retry_after else {}
        return urllib.error.HTTPError(
            "http://x", code, f"HTTP {code}", hdrs, io.BytesIO(b""),
        )

    def test_429_with_retry_after_respected(self):
        call_count = [0]

        def side_effect(req, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                raise self._make_http_error(429, retry_after="5")
            return _make_resp(json.dumps({"properties": {}}).encode("utf-8"))

        with patch("ihealth.publishers.notion.urllib.request.urlopen", side_effect=side_effect), \
             patch("ihealth.publishers.notion.time.sleep") as mock_sleep:
            c = NotionClient("t", "db_id")
            c.fetch_database_schema()
        self.assertEqual(call_count[0], 2)
        self.assertEqual(mock_sleep.call_count, 1)
        # Retry-After=5 が respected (バックオフではなく Retry-After 優先)
        self.assertAlmostEqual(mock_sleep.call_args[0][0], 5.0, places=3)

    def test_429_without_retry_after_uses_backoff(self):
        call_count = [0]

        def side_effect(req, timeout=None):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise self._make_http_error(429)  # Retry-After 無し
            return _make_resp(json.dumps({"properties": {}}).encode("utf-8"))

        with patch("ihealth.publishers.notion.urllib.request.urlopen", side_effect=side_effect), \
             patch("ihealth.publishers.notion.time.sleep") as mock_sleep:
            c = NotionClient("t", "db_id")
            c.fetch_database_schema()
        self.assertEqual(mock_sleep.call_count, 2)
        # 指数バックオフなので wait は 0 より大きい (ジッタで揺らぐ)
        for call in mock_sleep.call_args_list:
            self.assertGreater(call[0][0], 0)

    def test_500_retried(self):
        call_count = [0]

        def side_effect(req, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                raise self._make_http_error(500)
            return _make_resp(json.dumps({"properties": {}}).encode("utf-8"))

        with patch("ihealth.publishers.notion.urllib.request.urlopen", side_effect=side_effect), \
             patch("ihealth.publishers.notion.time.sleep"):
            c = NotionClient("t", "db_id")
            c.fetch_database_schema()
        self.assertEqual(call_count[0], 2)

    def test_4xx_not_429_not_retried(self):
        call_count = [0]

        def side_effect(req, timeout=None):
            call_count[0] += 1
            raise self._make_http_error(400)

        with patch("ihealth.publishers.notion.urllib.request.urlopen", side_effect=side_effect), \
             patch("ihealth.publishers.notion.time.sleep"):
            c = NotionClient("t", "db_id")
            with self.assertRaises(NotionError) as ctx:
                c.fetch_database_schema()
        self.assertEqual(call_count[0], 1)
        self.assertEqual(ctx.exception.status, 400)

    def test_url_error_retried(self):
        call_count = [0]

        def side_effect(req, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                raise urllib.error.URLError("connection refused")
            return _make_resp(json.dumps({"properties": {}}).encode("utf-8"))

        with patch("ihealth.publishers.notion.urllib.request.urlopen", side_effect=side_effect), \
             patch("ihealth.publishers.notion.time.sleep"):
            c = NotionClient("t", "db_id")
            c.fetch_database_schema()
        self.assertEqual(call_count[0], 2)

    def test_exhausted_retries_raises(self):
        call_count = [0]

        def side_effect(req, timeout=None):
            call_count[0] += 1
            raise self._make_http_error(429, retry_after="0.01")

        with patch("ihealth.publishers.notion.urllib.request.urlopen", side_effect=side_effect), \
             patch("ihealth.publishers.notion.time.sleep"):
            c = NotionClient("t", "db_id")
            with self.assertRaises(NotionError) as ctx:
                c.fetch_database_schema()
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(call_count[0], 3)  # _MAX_ATTEMPTS = 3


class TestParseRetryAfter(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertIsNone(_parse_retry_after(None))
        self.assertIsNone(_parse_retry_after(""))
        self.assertIsNone(_parse_retry_after("  "))

    def test_seconds_format(self):
        self.assertEqual(_parse_retry_after("30"), 30.0)

    def test_zero_treated_as_none(self):
        self.assertIsNone(_parse_retry_after("0"))

    def test_negative_treated_as_none(self):
        self.assertIsNone(_parse_retry_after("-5"))

    def test_nan_and_infinity_treated_as_none(self):
        for v in ("nan", "NaN", "inf", "Infinity", "-inf"):
            self.assertIsNone(
                _parse_retry_after(v), f"{v} should be None",
            )

    def test_upper_bound_clamp(self):
        # 600 秒は clamp 内、99999 秒は上限 (_MAX_RETRY_AFTER_SEC=60) に clamp
        self.assertEqual(_parse_retry_after("59"), 59.0)
        self.assertEqual(_parse_retry_after("99999"), 60.0)

    def test_http_date_future(self):
        import email.utils
        import datetime as dt
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=20)
        v = _parse_retry_after(email.utils.format_datetime(future))
        self.assertIsNotNone(v)
        self.assertGreaterEqual(v, 15)
        self.assertLessEqual(v, 25)

    def test_http_date_past_treated_as_none(self):
        import email.utils
        import datetime as dt
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60)
        # 過去日時は None フォールバック (sleep(0) で即時 burst retry を防ぐ)
        self.assertIsNone(_parse_retry_after(email.utils.format_datetime(past)))


class TestShouldRetryHttp(unittest.TestCase):
    def test_retryable(self):
        for code in (429, 500, 502, 503, 504):
            self.assertTrue(_should_retry_http(code), f"{code} should retry")

    def test_non_retryable(self):
        for code in (400, 401, 403, 404, 200, 301):
            self.assertFalse(_should_retry_http(code), f"{code} should not retry")


class TestBuildProperties(unittest.TestCase):
    def setUp(self):
        self.schema = {prop_name: "number" for prop_name, _ in PROPERTY_MAP.values()}

    def test_maps_all_populated_fields(self):
        data = DailyHealthData(
            date=date(2026, 4, 22),
            step_count=2986,
            sleep_hours=4.033,
            nap_hours=1.567,
            body_mass_kg=65.55,
        )
        props = build_properties(data, self.schema)
        # 睡眠は hours → 分の整数変換 (英語 default = "Sleep Minutes")
        self.assertEqual(props["Sleep Minutes"], {"number": 242})
        self.assertEqual(props["Nap Minutes"], {"number": 94})
        self.assertEqual(props["Body Mass (kg)"], {"number": 65.5})
        self.assertEqual(props["Step Count"], {"number": 2986})

    def test_none_fields_skipped(self):
        data = DailyHealthData(date=date(2026, 4, 22), step_count=100)
        props = build_properties(data, self.schema)
        # 他のフィールドは all None なので props に入らない
        self.assertEqual(list(props.keys()), ["Step Count"])

    def test_db_missing_property_warn_and_skip(self):
        data = DailyHealthData(
            date=date(2026, 4, 22),
            step_count=100,
            body_mass_kg=65.0,
        )
        partial_schema = {"Step Count": "number"}  # Body Mass は DB に無い
        # warning ログが実際に出ているかも検証 (単に skip するだけでは
        # 運用時の「DB 未設定」に気付けないため warning は契約の一部)
        import logging as _logging
        with self.assertLogs("ihealth.publishers.notion", level=_logging.WARNING) as cm:
            props = build_properties(data, partial_schema)
        self.assertIn("Step Count", props)
        self.assertNotIn("Body Mass (kg)", props)
        # warning メッセージに欠落プロパティ名が含まれていること
        self.assertTrue(
            any("Body Mass (kg)" in line for line in cm.output),
            f"warning ログに 'Body Mass (kg)' が含まれていない: {cm.output}",
        )


class TestBuildPropertiesOverrides(unittest.TestCase):
    """``property_overrides`` 引数 (Phase 1 B1 / Issue #18) の挙動.

    config.toml 由来の Notion プロパティ名 override が正しく適用されるか、
    また override されないフィールドは既定値 (PROPERTY_MAP) のままか
    を保証する.
    """

    def test_override_replaces_property_name(self):
        """override 指定があるフィールドは Notion プロパティ名が差し替わる."""
        data = DailyHealthData(date=date(2026, 4, 22), step_count=2986)
        overrides = {"step_count": "Steps (count)"}  # 英語版 schema を想定
        schema = {"Steps (count)": "number"}
        props = build_properties(
            data, schema, property_overrides=overrides,
        )
        self.assertEqual(props, {"Steps (count)": {"number": 2986}})

    def test_override_with_partial_keys_keeps_defaults(self):
        """override が一部だけのとき、未指定フィールドは既定値が使われる."""
        data = DailyHealthData(
            date=date(2026, 4, 22),
            step_count=100,
            body_mass_kg=65.0,
        )
        overrides = {"step_count": "Steps"}  # body_mass_kg は override なし
        schema = {"Steps": "number", "Body Mass (kg)": "number"}  # 既定英語名も含む
        props = build_properties(
            data, schema, property_overrides=overrides,
        )
        self.assertEqual(props["Steps"], {"number": 100})
        self.assertEqual(props["Body Mass (kg)"], {"number": 65.0})

    def test_override_none_falls_back_to_defaults(self):
        """``property_overrides=None`` は既存挙動と等価."""
        data = DailyHealthData(date=date(2026, 4, 22), step_count=100)
        schema = {"Step Count": "number"}
        props_with_none = build_properties(
            data, schema, property_overrides=None,
        )
        props_without_arg = build_properties(data, schema)
        self.assertEqual(props_with_none, props_without_arg)

    def test_override_to_property_not_in_schema_skipped(self):
        """override 後のプロパティ名が DB schema に無ければ skip + warning."""
        data = DailyHealthData(date=date(2026, 4, 22), step_count=100)
        overrides = {"step_count": "NoSuchProp"}
        schema = {"Step Count": "number"}  # 既定英語名のみ
        import logging as _logging
        with self.assertLogs("ihealth.publishers.notion", level=_logging.WARNING) as cm:
            props = build_properties(
                data, schema, property_overrides=overrides,
            )
        self.assertEqual(props, {})  # override 後の名前が schema に無いので空
        self.assertTrue(
            any("NoSuchProp" in line for line in cm.output),
            f"warning に NoSuchProp が含まれていない: {cm.output}",
        )


class TestDatePropertyOverride(unittest.TestCase):
    """Phase 1 B2: NotionClient.date_property_name の構築時 override."""

    def test_default_is_english_date(self):
        c = NotionClient("t", "db_id")
        self.assertEqual(c.date_property_name, "Date")

    def test_override_via_constructor(self):
        c = NotionClient("t", "db_id", date_property_name="日付")
        self.assertEqual(c.date_property_name, "日付")

    def test_default_via_none(self):
        c = NotionClient("t", "db_id", date_property_name=None)
        self.assertEqual(c.date_property_name, NotionClient.DEFAULT_DATE_PROPERTY_NAME)

    def test_find_page_uses_override(self):
        body = json.dumps({"results": [{"id": "abc"}], "has_more": False}).encode("utf-8")
        with patch("ihealth.publishers.notion.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_resp(body)
            c = NotionClient("t", "db_id", date_property_name="日付")
            c.find_page_by_date(date(2026, 4, 22))
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data)
        self.assertEqual(payload["filter"]["property"], "日付")

    def test_empty_string_rejected(self):
        # codex round 1 PR #26 nice-to-have: silent fallback 防止
        with self.assertRaises(ValueError):
            NotionClient("t", "db_id", date_property_name="")

    def test_whitespace_only_rejected(self):
        with self.assertRaises(ValueError):
            NotionClient("t", "db_id", date_property_name="   ")

    def test_strip_applied(self):
        # 前後空白は trim される (空白だけは拒否, 中間に空白は許容)
        c = NotionClient("t", "db_id", date_property_name="  Date  ")
        self.assertEqual(c.date_property_name, "Date")

    def test_legacy_class_const_alias(self):
        # codex round 1 PR #26 nice-to-have: 旧 ``DATE_PROPERTY_NAME`` alias
        self.assertEqual(
            NotionClient.DATE_PROPERTY_NAME,
            NotionClient.DEFAULT_DATE_PROPERTY_NAME,
        )


class TestPropertyMapIntegrity(unittest.TestCase):
    def test_all_fields_match_model(self):
        from dataclasses import fields as dataclass_fields
        model_fields = {f.name for f in dataclass_fields(DailyHealthData)} - {"date"}
        map_fields = set(PROPERTY_MAP.keys())
        self.assertEqual(model_fields, map_fields)


if __name__ == "__main__":
    unittest.main()
