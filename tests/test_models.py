"""DailyHealthData の妥当性検証 + iter_fields / as_dict の回帰テスト (Issue #15)。"""

from __future__ import annotations

import unittest
from datetime import date
from typing import Any

from ihealth.models import DailyHealthData


class TestValidation(unittest.TestCase):
    """__post_init__ で生理学的妥当範囲を検証する。違反は ValueError。"""

    def test_defaults_all_none_ok(self):
        # 全フィールド None (欠損のみ) は検証通過
        DailyHealthData(date=date(2026, 4, 22))

    def test_negative_step_count_raises(self):
        with self.assertRaises(ValueError):
            DailyHealthData(date=date(2026, 4, 22), step_count=-1)

    def test_negative_distance_raises(self):
        with self.assertRaises(ValueError):
            DailyHealthData(date=date(2026, 4, 22), distance_km=-0.1)

    def test_impossible_heart_rate_too_high_raises(self):
        with self.assertRaises(ValueError):
            DailyHealthData(date=date(2026, 4, 22), heart_rate_avg=300.0)

    def test_impossible_heart_rate_too_low_raises(self):
        with self.assertRaises(ValueError):
            DailyHealthData(date=date(2026, 4, 22), heart_rate_avg=10.0)

    def test_resting_heart_rate_above_150_raises(self):
        # 安静時心拍の上限は 150 (一般の heart_rate_avg/max より厳しい)
        with self.assertRaises(ValueError):
            DailyHealthData(date=date(2026, 4, 22), heart_rate_resting=200.0)

    def test_oxygen_saturation_above_100_raises(self):
        with self.assertRaises(ValueError):
            DailyHealthData(date=date(2026, 4, 22), oxygen_saturation=105.0)

    def test_oxygen_saturation_below_50_raises(self):
        with self.assertRaises(ValueError):
            DailyHealthData(date=date(2026, 4, 22), oxygen_saturation=40.0)

    def test_body_fat_above_60_raises(self):
        with self.assertRaises(ValueError):
            DailyHealthData(date=date(2026, 4, 22), body_fat_percentage=150.0)

    def test_body_fat_below_1_raises(self):
        with self.assertRaises(ValueError):
            DailyHealthData(date=date(2026, 4, 22), body_fat_percentage=0.5)

    def test_body_mass_outside_range_raises(self):
        with self.assertRaises(ValueError):
            DailyHealthData(date=date(2026, 4, 22), body_mass_kg=5.0)
        with self.assertRaises(ValueError):
            DailyHealthData(date=date(2026, 4, 22), body_mass_kg=500.0)

    def test_sleep_hours_above_24_raises(self):
        with self.assertRaises(ValueError):
            DailyHealthData(date=date(2026, 4, 22), sleep_hours=25.0)

    def test_mindful_minutes_over_1440_raises(self):
        # 1 日 1440 分を超える瞑想は不可能
        with self.assertRaises(ValueError):
            DailyHealthData(date=date(2026, 4, 22), mindful_minutes=1500.0)

    def test_avg_exceeds_max_raises(self):
        # heart_rate_avg > heart_rate_max は物理的に不整合
        with self.assertRaises(ValueError):
            DailyHealthData(
                date=date(2026, 4, 22),
                heart_rate_avg=150.0,
                heart_rate_max=140.0,
            )

    def test_realistic_values_pass(self):
        # 実データの典型値
        DailyHealthData(
            date=date(2026, 4, 22),
            step_count=2986,
            distance_km=2.94,
            active_energy_kcal=440.6,
            heart_rate_avg=131.5,
            heart_rate_max=167.0,
            heart_rate_resting=64.0,
            oxygen_saturation=97.7,
            sleep_hours=4.03,
            nap_hours=1.57,
            body_mass_kg=65.55,
            body_fat_percentage=19.8,
        )


class TestIterFields(unittest.TestCase):
    def test_iter_returns_15_fields(self):
        d = DailyHealthData(date=date(2026, 4, 22), step_count=100)
        fields = list(d.iter_fields())
        # date + 14 metric fields (CLAUDE.md では "13 メトリクス" と書かれるが、
        # heart_rate が avg/max/resting の 3 つに分解されていて mindful も
        # sessions/minutes の 2 つなので、models.py 上の属性数は 14 + date)
        self.assertEqual(len(fields), 15)
        # 最初は date
        self.assertEqual(fields[0][0], "date")
        self.assertEqual(fields[0][1], date(2026, 4, 22))
        # step_count が含まれている
        names = [name for name, _ in fields]
        self.assertIn("step_count", names)

    def test_iter_yields_none_and_non_none(self):
        d = DailyHealthData(
            date=date(2026, 4, 22),
            step_count=100,
            body_mass_kg=65.0,
            # その他は None
        )
        got = dict(d.iter_fields())
        self.assertEqual(got["step_count"], 100)
        self.assertEqual(got["body_mass_kg"], 65.0)
        self.assertIsNone(got["heart_rate_avg"])


class TestAsDict(unittest.TestCase):
    def test_as_dict_date_is_iso(self):
        d = DailyHealthData(date=date(2026, 4, 22), step_count=100)
        data = d.as_dict()
        self.assertEqual(data["date"], "2026-04-22")
        self.assertEqual(data["step_count"], 100)
        self.assertIsNone(data["heart_rate_avg"])


if __name__ == "__main__":
    unittest.main()
