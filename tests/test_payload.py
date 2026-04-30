"""Publisher 共通の payload 検証 / coercion (Phase 1 A3-stdout) の回帰テスト.

``_payload`` は publisher 間の internal API で、外部からは import されない
想定だが、責務が独立してテストしやすいので単体テストを切る.
"""

from __future__ import annotations

import unittest
from datetime import date

from ihealth.models import DailyHealthData
from ihealth.publishers._payload import (
    FIELD_SPECS,
    PayloadValidationError,
    build_payload,
    coerce_float,
    coerce_int,
    convert_field,
)


class TestFieldSpecs(unittest.TestCase):
    def test_all_fields_covered(self):
        from dataclasses import fields as dataclass_fields
        model_fields = {f.name for f in dataclass_fields(DailyHealthData)} - {"date"}
        spec_fields = set(FIELD_SPECS.keys())
        self.assertEqual(model_fields, spec_fields)

    def test_field_specs_is_read_only(self):
        """codex round 1 PR #22 指摘: shared mutable state を防ぐため
        :class:`MappingProxyType` で read-only view 化されている.
        """
        with self.assertRaises(TypeError):
            FIELD_SPECS["new_field"] = "anything"  # type: ignore[index]
        with self.assertRaises(TypeError):
            del FIELD_SPECS["step_count"]  # type: ignore[attr-defined]

    def test_markdown_alias_is_same_view(self):
        # markdown.MARKDOWN_FIELD_SPECS は _payload.FIELD_SPECS の alias.
        # 同じオブジェクトを指していて、こちらも read-only.
        from ihealth.publishers.markdown import MARKDOWN_FIELD_SPECS
        self.assertIs(MARKDOWN_FIELD_SPECS, FIELD_SPECS)
        with self.assertRaises(TypeError):
            MARKDOWN_FIELD_SPECS["new"] = None  # type: ignore[index]


class TestCoerceInt(unittest.TestCase):
    def test_int_passthrough(self):
        self.assertEqual(coerce_int("step_count", 12345), 12345)

    def test_bool_rejected(self):
        with self.assertRaises(PayloadValidationError):
            coerce_int("step_count", True)
        with self.assertRaises(PayloadValidationError):
            coerce_int("step_count", False)

    def test_float_rejected(self):
        with self.assertRaises(PayloadValidationError):
            coerce_int("step_count", 1.9)

    def test_str_rejected(self):
        with self.assertRaises(PayloadValidationError):
            coerce_int("step_count", "5")

    def test_none_rejected(self):
        with self.assertRaises(PayloadValidationError):
            coerce_int("step_count", None)


class TestCoerceFloat(unittest.TestCase):
    def test_float_passthrough(self):
        self.assertEqual(coerce_float("distance_km", 5.234, 2), 5.23)

    def test_int_promoted(self):
        self.assertEqual(coerce_float("heart_rate_avg", 70, 1), 70.0)

    def test_bool_rejected(self):
        with self.assertRaises(PayloadValidationError):
            coerce_float("distance_km", True, 2)

    def test_str_rejected(self):
        with self.assertRaises(PayloadValidationError):
            coerce_float("distance_km", "5.0", 2)

    def test_nan_rejected(self):
        with self.assertRaises(PayloadValidationError):
            coerce_float("distance_km", float("nan"), 2)

    def test_inf_rejected(self):
        with self.assertRaises(PayloadValidationError):
            coerce_float("distance_km", float("inf"), 2)
        with self.assertRaises(PayloadValidationError):
            coerce_float("distance_km", float("-inf"), 2)


class TestConvertField(unittest.TestCase):
    def test_int_kind(self):
        spec = FIELD_SPECS["step_count"]
        self.assertEqual(convert_field("step_count", 100, spec), 100)
        with self.assertRaises(PayloadValidationError):
            convert_field("step_count", 1.5, spec)

    def test_float_kind(self):
        spec = FIELD_SPECS["distance_km"]
        self.assertEqual(convert_field("distance_km", 5.236, spec), 5.24)


class TestBuildPayload(unittest.TestCase):
    def test_only_date(self):
        data = DailyHealthData(date=date(2026, 4, 22))
        payload = build_payload(data)
        self.assertEqual(payload, {"date": "2026-04-22"})

    def test_with_fields(self):
        data = DailyHealthData(
            date=date(2026, 4, 22),
            step_count=12345,
            distance_km=5.234,
            sleep_hours=7.234,
        )
        payload = build_payload(data)
        self.assertEqual(payload["date"], "2026-04-22")
        self.assertEqual(payload["step_count"], 12345)
        self.assertEqual(payload["distance_km"], 5.23)  # round 2
        self.assertEqual(payload["sleep_hours"], 7.23)  # round 2

    def test_none_fields_skipped(self):
        data = DailyHealthData(date=date(2026, 4, 22), step_count=100)
        payload = build_payload(data)
        self.assertEqual(set(payload.keys()), {"date", "step_count"})

    def test_field_order_date_first(self):
        # 何が populated かに関係なく date が先頭 (JSON で読みやすく)
        data = DailyHealthData(
            date=date(2026, 4, 22), body_mass_kg=65.0, step_count=100,
        )
        payload = build_payload(data)
        keys = list(payload.keys())
        self.assertEqual(keys[0], "date")
        # FIELD_SPECS の宣言順 (step_count が body_mass_kg より早い)
        self.assertLess(keys.index("step_count"), keys.index("body_mass_kg"))

    def test_invalid_value_raises(self):
        # bool が混じった parser バグ等 → fail-fast
        data = DailyHealthData.__new__(DailyHealthData)
        object.__setattr__(data, "date", date(2026, 4, 22))
        for f in FIELD_SPECS:
            object.__setattr__(data, f, None)
        object.__setattr__(data, "step_count", True)
        with self.assertRaises(PayloadValidationError) as ctx:
            build_payload(data)
        self.assertIn("step_count", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
