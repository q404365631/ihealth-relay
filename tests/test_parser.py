"""parser.py の 13 集計関数 + parse_all の回帰テスト。

unittest 標準ライブラリのみ。fixture は ``tests/fixtures/sample_health_export/``
の evenly サンプル (Issue #7 で生成) を使用。

集計値の絶対値は間引きで変わるので、**型と None/not-None の挙動** を主軸に検証する。
特定の値が必要なケース (sleep_hours=4.033, body_mass_kg=65.55 など、元データが
100 件未満で間引き影響を受けないもの) は期待値を直接比較する。
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ihealth.models import DailyHealthData
from ihealth.parser import (
    ParseContext,
    parse_active_energy_kcal,
    parse_all,
    parse_body_fat_percentage,
    parse_body_mass_kg,
    parse_distance_km,
    parse_exercise_intensity_score,
    parse_heart_rate_avg,
    parse_heart_rate_max,
    parse_heart_rate_resting,
    parse_mindful_minutes,
    parse_mindful_sessions,
    parse_nap_hours,
    parse_oxygen_saturation,
    parse_sleep_hours,
    parse_step_count,
)


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "sample_health_export"

_MAC_EPOCH_UTC = datetime(2001, 1, 1, tzinfo=timezone.utc)
_JST = timezone(timedelta(hours=9))


def _mac_seconds_at_jst(year: int, month: int, day: int, hour: int) -> float:
    """JST の ``(year, month, day, hour)`` を Mac absolute time (秒) に変換する。

    parser.py が入力として扱う Apple HealthKit のタイムスタンプ形式 (Mac epoch 起点)
    をテスト側で組み立てるためのヘルパー。
    """
    dt = datetime(year, month, day, hour, 0, 0, tzinfo=_JST)
    return (dt - _MAC_EPOCH_UTC).total_seconds()


class TestStepCount(unittest.TestCase):
    CTX = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)

    def test_count_unit_only(self):
        payload = {"data": [
            {"qty": 1.0, "unit": "count"},
            {"qty": 100.0, "unit": "bogus"},
            {"qty": 2.0, "unit": "count"},
        ]}
        self.assertEqual(parse_step_count(payload, self.CTX), 3)

    def test_empty(self):
        self.assertIsNone(parse_step_count({"data": []}, self.CTX))

    def test_no_data_key(self):
        self.assertIsNone(parse_step_count({}, self.CTX))

    def test_sum_is_rounded_to_int(self):
        payload = {"data": [{"qty": 1.4, "unit": "count"}, {"qty": 1.3, "unit": "count"}]}
        # 2.7 → round(2.7) = 3
        self.assertEqual(parse_step_count(payload, self.CTX), 3)


class TestDistanceKm(unittest.TestCase):
    CTX = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)

    def test_km_unit_only(self):
        payload = {"data": [
            {"qty": 1.5, "unit": "km"},
            {"qty": 999.0, "unit": "m"},  # m は捨てる
            {"qty": 0.5, "unit": "km"},
        ]}
        self.assertAlmostEqual(parse_distance_km(payload, self.CTX), 2.0)

    def test_empty(self):
        self.assertIsNone(parse_distance_km({"data": []}, self.CTX))


class TestActiveEnergyKcal(unittest.TestCase):
    CTX = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)

    def test_kcal_only_kJ_ignored(self):
        payload = {"data": [
            {"qty": 10.0, "unit": "kcal"},
            {"qty": 999.0, "unit": "kJ"},  # kJ は重複出力なので捨てる
            {"qty": 5.5, "unit": "kcal"},
        ]}
        self.assertAlmostEqual(parse_active_energy_kcal(payload, self.CTX), 15.5)

    def test_empty(self):
        self.assertIsNone(parse_active_energy_kcal({"data": []}, self.CTX))


class TestExerciseIntensityScore(unittest.TestCase):
    """Heart Points (Issue #13): 1 分 bucket 集約 → bucket 平均 avg → zone 倍率。

    heart_rate.hae は 1 秒瞬間サンプルの集合なので、各サンプルを ``int(start // 60)``
    で 1 分 bucket に振り分け、bucket 内平均 avg で zone 判定する。
    """

    def _ctx(self, max_hr: float = 178.0) -> ParseContext:
        return ParseContext(target_date=date(2026, 4, 22), max_heart_rate=max_hr)

    def test_vigorous_bucket_2pt(self):
        # max_hr=180 → 高強度閾値 180*0.7 = 126
        # avg=140 の 1 サンプル bucket 0 → vigorous → 2 pt
        payload = {"data": [{"avg": 140, "start": 0, "end": 1}]}
        self.assertAlmostEqual(
            parse_exercise_intensity_score(payload, self._ctx(180.0)), 2.0
        )

    def test_moderate_bucket_1pt(self):
        # max_hr=180 → 中強度閾値 180*0.5 = 90, 高強度 126
        # avg=100 → moderate → 1 pt
        payload = {"data": [{"avg": 100, "start": 0, "end": 1}]}
        self.assertAlmostEqual(
            parse_exercise_intensity_score(payload, self._ctx(180.0)), 1.0
        )

    def test_low_bucket_0pt(self):
        payload = {"data": [{"avg": 80, "start": 0, "end": 1}]}
        # low zone → None (0 pt)
        self.assertIsNone(parse_exercise_intensity_score(payload, self._ctx(180.0)))

    def test_missing_fields_skipped(self):
        payload = {"data": [
            {"avg": 140},                 # start 欠落 → 無視
            {"start": 0, "end": 1},       # avg 欠落 → 無視
            {"avg": 140, "start": 0, "end": 1},  # これだけ有効
        ]}
        self.assertAlmostEqual(
            parse_exercise_intensity_score(payload, self._ctx(180.0)), 2.0
        )

    def test_same_bucket_samples_averaged(self):
        # 同一 bucket (start=0 と start=30) 内の avg を平均してから zone 判定
        # (100 + 140) / 2 = 120、threshold vigorous=126 未満なので moderate → 1 pt
        payload = {"data": [
            {"avg": 100, "start": 0,  "end": 1},
            {"avg": 140, "start": 30, "end": 31},
        ]}
        self.assertEqual(parse_exercise_intensity_score(payload, self._ctx(180.0)), 1.0)

    def test_duplicate_samples_same_bucket_not_double_counted(self):
        # Apple Watch + iPhone など複数ソースから同 bucket に 3 サンプル投入
        # 平均は 140 のまま → vigorous × 1 bucket = 2 pt (二重計上しない)
        payload = {"data": [
            {"avg": 140, "start": 0, "end": 1},
            {"avg": 140, "start": 0, "end": 1},
            {"avg": 140, "start": 0, "end": 1},
        ]}
        self.assertEqual(parse_exercise_intensity_score(payload, self._ctx(180.0)), 2.0)

    def test_multiple_buckets_sum(self):
        # 3 bucket にまたがる:
        #   bucket 0: avg 140 (vigorous) → 2 pt
        #   bucket 1: avg 100 (moderate) → 1 pt
        #   bucket 2: avg  60 (low)      → 0 pt
        payload = {"data": [
            {"avg": 140, "start":   0, "end":   1},
            {"avg": 100, "start":  60, "end":  61},
            {"avg":  60, "start": 120, "end": 121},
        ]}
        self.assertEqual(parse_exercise_intensity_score(payload, self._ctx(180.0)), 3.0)

    def test_bucket_index_uses_start_only(self):
        # end が次の bucket に跨っても bucket index は start で決まる
        # start=59 → bucket 0 (59//60=0)、start=60 → bucket 1 (60//60=1)
        payload = {"data": [
            {"avg": 140, "start": 59, "end":  62},
            {"avg": 140, "start": 60, "end":  63},
        ]}
        # 2 bucket × 2 pt = 4 pt
        self.assertEqual(parse_exercise_intensity_score(payload, self._ctx(180.0)), 4.0)

    def test_conflicting_sources_averaged_may_drop_zone(self):
        # 実運用注意: 複数ソース (Apple Watch=140, iPhone=80) の異種値は同 bucket で
        # 単純平均 (110) → moderate zone に落ちる。vigorous ではなく 1 pt になる
        # (本実装の既知の弱点で、docstring に明示済み)。
        payload = {"data": [
            {"avg": 140, "start": 0, "end": 1},  # Apple Watch (vigorous)
            {"avg":  80, "start": 0, "end": 1},  # iPhone (low)
        ]}
        # mean=110 → threshold_moderate=90 (180*0.5), threshold_vigorous=126 (180*0.7)
        # 110 は moderate zone → 1 pt
        self.assertEqual(parse_exercise_intensity_score(payload, self._ctx(180.0)), 1.0)

    def test_large_mac_epoch_bucket(self):
        # 実データの Mac epoch 秒 (2026 年 = 約 8 億) で bucket 計算が正しく動く
        # (float 精度 / 整数除算の境界確認)
        base = 798_500_000  # 2026-04-22 頃の Mac epoch 秒
        payload = {"data": [
            {"avg": 140, "start": base,      "end": base + 1},   # bucket k
            {"avg": 140, "start": base + 30, "end": base + 31},  # 同 bucket k
            {"avg": 140, "start": base + 60, "end": base + 61},  # bucket k+1
        ]}
        # 2 bucket × 2 pt = 4 pt
        self.assertEqual(parse_exercise_intensity_score(payload, self._ctx(180.0)), 4.0)


class TestHeartRateAvg(unittest.TestCase):
    CTX = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)

    def test_mean_of_avg(self):
        payload = {"data": [{"avg": 60.0}, {"avg": 80.0}, {"avg": 100.0}]}
        self.assertAlmostEqual(parse_heart_rate_avg(payload, self.CTX), 80.0)

    def test_empty(self):
        self.assertIsNone(parse_heart_rate_avg({"data": []}, self.CTX))


class TestHeartRateMax(unittest.TestCase):
    CTX = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)

    def test_max_of_max(self):
        payload = {"data": [{"max": 60.0}, {"max": 80.0}, {"max": 120.0}]}
        self.assertAlmostEqual(parse_heart_rate_max(payload, self.CTX), 120.0)


class TestHeartRateResting(unittest.TestCase):
    CTX = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)

    def test_count_per_min_unit_only(self):
        payload = {"data": [
            {"qty": 60.0, "unit": "count/min"},
            {"qty": 999.0, "unit": "bpm"},  # 違う単位は捨てる
            {"qty": 64.0, "unit": "count/min"},
        ]}
        self.assertAlmostEqual(parse_heart_rate_resting(payload, self.CTX), 62.0)

    def test_missing_qty_skipped(self):
        payload = {"data": [
            {"unit": "count/min"},  # qty なし
            {"qty": 60.0, "unit": "count/min"},
        ]}
        self.assertAlmostEqual(parse_heart_rate_resting(payload, self.CTX), 60.0)


class TestOxygenSaturation(unittest.TestCase):
    CTX = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)

    def test_prefers_avg_over_qty(self):
        payload = {"data": [
            {"avg": 97.0, "qty": 999.0, "unit": "%"},  # avg 優先
            {"qty": 95.0, "unit": "%"},                 # avg なし → qty
        ]}
        self.assertAlmostEqual(parse_oxygen_saturation(payload, self.CTX), 96.0)

    def test_non_percent_unit_skipped(self):
        payload = {"data": [
            {"avg": 97.0, "unit": "%"},
            {"avg": 42.0, "unit": "mmol"},  # 無関係単位
        ]}
        self.assertAlmostEqual(parse_oxygen_saturation(payload, self.CTX), 97.0)


class TestSleepHours(unittest.TestCase):
    """端的ケース: 同じ睡眠期間を AutoSleep + Apple Watch 両方で投入し、
    「ソース別 SUM → 最大ソース」の二重計上回避ロジックを確認する。"""

    CTX = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)

    def test_night_main_sleep_with_duplicate_sources(self):
        # JST 08:00 に起床した asleep レコードを 2 ソースで重複投入
        end_08jst = _mac_seconds_at_jst(2026, 4, 22, 8)
        payload = {"data": [
            {
                "asleep": 4.0,
                "end": end_08jst,
                "sources": [{"identifier": "com.sleepmatic.watchapp"}],
            },
            # 同時刻、別ソースで 3 時間を投入 (AutoSleep)
            {
                "asleep": 3.0,
                "end": end_08jst,
                "sources": [{"identifier": "com.lonewolfdevelopment.lwds"}],
            },
        ]}
        # 4.0 (最大ソース) のみ採用、単純 SUM(7.0) ではない
        self.assertAlmostEqual(parse_sleep_hours(payload, self.CTX), 4.0)

    def test_nap_hour_rejected(self):
        # JST 16:00 に終了 → 昼寝扱い、sleep_hours には入らない
        end_16jst = _mac_seconds_at_jst(2026, 4, 22, 16)
        payload = {"data": [
            {"asleep": 1.5, "end": end_16jst, "sources": [{"identifier": "watch"}]},
        ]}
        self.assertIsNone(parse_sleep_hours(payload, self.CTX))

    def test_sum_within_same_source(self):
        # 同一ソースから 2 レコード → 合計 (二重計上ではない)
        end_08jst = _mac_seconds_at_jst(2026, 4, 22, 8)
        payload = {"data": [
            {"asleep": 2.0, "end": end_08jst, "sources": [{"identifier": "watch"}]},
            {"asleep": 2.0, "end": end_08jst, "sources": [{"identifier": "watch"}]},
        ]}
        self.assertAlmostEqual(parse_sleep_hours(payload, self.CTX), 4.0)

    def test_boundary_14_rejected(self):
        # JST 14:00 ちょうど → sleep_hours の範囲 [0,14) から外れる (上限 exclusive)
        end_14jst = _mac_seconds_at_jst(2026, 4, 22, 14)
        payload = {"data": [
            {"asleep": 2.0, "end": end_14jst, "sources": [{"identifier": "watch"}]},
        ]}
        self.assertIsNone(parse_sleep_hours(payload, self.CTX))

    def test_boundary_13_accepted(self):
        # JST 13:00 (範囲 [0,14) の内側最終境界) → sleep_hours に入る
        end_13jst = _mac_seconds_at_jst(2026, 4, 22, 13)
        payload = {"data": [
            {"asleep": 2.0, "end": end_13jst, "sources": [{"identifier": "watch"}]},
        ]}
        self.assertAlmostEqual(parse_sleep_hours(payload, self.CTX), 2.0)

    # Issue #14: end の JST 日付フィルタ (前日ファイル併読対策)

    def test_previous_night_record_included(self):
        # 前夜 23:15 → 当日 04:03 の主睡眠。end は当日 04:03 JST で target_date に
        # 一致、時刻は 4 時 → [0, 14) 範囲内 → sleep_hours に入る
        end_at = _mac_seconds_at_jst(2026, 4, 22, 4)  # JST 04:00
        payload = {"data": [
            {"asleep": 4.8, "end": end_at, "sources": [{"identifier": "watch"}]},
        ]}
        self.assertAlmostEqual(parse_sleep_hours(payload, self.CTX), 4.8)

    def test_next_day_end_excluded(self):
        # 当日 23:30 就寝 → 翌日 (2026-04-23) 06:00 起床のレコード。
        # end の日付が 2026-04-23 なので target_date=2026-04-22 としては対象外。
        # (前日ファイル併読で他日レコードが混入する可能性があるため日付フィルタで弾く)
        end_at = _mac_seconds_at_jst(2026, 4, 23, 6)
        payload = {"data": [
            {"asleep": 6.5, "end": end_at, "sources": [{"identifier": "watch"}]},
        ]}
        self.assertIsNone(parse_sleep_hours(payload, self.CTX))

    def test_previous_day_end_excluded(self):
        # 前日 05:00 起床 (end は 2026-04-21) → target_date=2026-04-22 では対象外
        end_at = _mac_seconds_at_jst(2026, 4, 21, 5)
        payload = {"data": [
            {"asleep": 4.0, "end": end_at, "sources": [{"identifier": "watch"}]},
        ]}
        self.assertIsNone(parse_sleep_hours(payload, self.CTX))

    def test_merged_payload_multiple_days_filtered(self):
        # 前日 + 当日を結合した payload に 3 つの異なる日付のレコード
        # target_date=2026-04-22 の end のみ採用
        payload = {"data": [
            # 前日分 (2026-04-21 05:00 起床) - 対象外
            {"asleep": 5.0, "end": _mac_seconds_at_jst(2026, 4, 21, 5),
             "sources": [{"identifier": "watch"}]},
            # 当日分 (2026-04-22 04:00 起床) - 対象
            {"asleep": 4.5, "end": _mac_seconds_at_jst(2026, 4, 22, 4),
             "sources": [{"identifier": "watch"}]},
            # 当日夜〜翌朝 (2026-04-23 06:00 起床) - 対象外
            {"asleep": 6.0, "end": _mac_seconds_at_jst(2026, 4, 23, 6),
             "sources": [{"identifier": "watch"}]},
        ]}
        self.assertAlmostEqual(parse_sleep_hours(payload, self.CTX), 4.5)


class TestNapHours(unittest.TestCase):
    CTX = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)

    def test_afternoon_nap_accepted(self):
        # JST 16:00 終了は昼寝
        end_16jst = _mac_seconds_at_jst(2026, 4, 22, 16)
        payload = {"data": [
            {"asleep": 1.0, "end": end_16jst, "sources": [{"identifier": "watch"}]},
        ]}
        self.assertAlmostEqual(parse_nap_hours(payload, self.CTX), 1.0)

    def test_morning_sleep_rejected(self):
        # JST 08:00 終了 → 夜主睡眠、nap ではない
        end_08jst = _mac_seconds_at_jst(2026, 4, 22, 8)
        payload = {"data": [
            {"asleep": 4.0, "end": end_08jst, "sources": [{"identifier": "watch"}]},
        ]}
        self.assertIsNone(parse_nap_hours(payload, self.CTX))

    def test_evening_rejected_boundary(self):
        # JST 20:00 ちょうど → nap 対象外 (上限は exclusive)
        end_20jst = _mac_seconds_at_jst(2026, 4, 22, 20)
        payload = {"data": [
            {"asleep": 1.0, "end": end_20jst, "sources": [{"identifier": "watch"}]},
        ]}
        self.assertIsNone(parse_nap_hours(payload, self.CTX))

    def test_boundary_14_accepted(self):
        # JST 14:00 ちょうど → nap_hours 範囲 [14, 20) の下限 inclusive
        end_14jst = _mac_seconds_at_jst(2026, 4, 22, 14)
        payload = {"data": [
            {"asleep": 1.0, "end": end_14jst, "sources": [{"identifier": "watch"}]},
        ]}
        self.assertAlmostEqual(parse_nap_hours(payload, self.CTX), 1.0)


class TestMindfulSessions(unittest.TestCase):
    CTX = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)

    def test_count(self):
        payload = {"data": [{}, {}, {}]}
        self.assertEqual(parse_mindful_sessions(payload, self.CTX), 3)

    def test_empty_is_none(self):
        self.assertIsNone(parse_mindful_sessions({"data": []}, self.CTX))


class TestMindfulMinutes(unittest.TestCase):
    CTX = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)

    def test_duration_sum(self):
        # (end - start) / 60 の合計 (秒 → 分)
        payload = {"data": [
            {"start": 0, "end": 600},    # 10 分
            {"start": 1000, "end": 1780},  # 13 分
        ]}
        self.assertAlmostEqual(parse_mindful_minutes(payload, self.CTX), 23.0)

    def test_total_zero_or_negative_returns_none(self):
        # 負数レコード (-10 分) と正レコード (+10 分) の合計は 0 → None
        # (parser は per-record で skip しないが、合計 <= 0 で None を返す契約)
        payload = {"data": [
            {"start": 600, "end": 0},
            {"start": 0, "end": 600},
        ]}
        self.assertIsNone(parse_mindful_minutes(payload, self.CTX))


class TestBodyMassKg(unittest.TestCase):
    CTX = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)

    def test_kg_only_lb_ignored(self):
        payload = {"data": [
            {"qty": 139.88, "unit": "lb", "end": 1000},
            {"qty": 63.45, "unit": "kg", "end": 1000},
            {"qty": 64.00, "unit": "kg", "end": 2000},  # end が新しい
        ]}
        # 最新 (end 最大) の kg を返す
        self.assertAlmostEqual(parse_body_mass_kg(payload, self.CTX), 64.00)

    def test_no_kg_records(self):
        payload = {"data": [{"qty": 140.0, "unit": "lb", "end": 1000}]}
        self.assertIsNone(parse_body_mass_kg(payload, self.CTX))


class TestBodyFatPercentage(unittest.TestCase):
    CTX = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)

    def test_latest_percent(self):
        payload = {"data": [
            {"qty": 18.5, "unit": "%", "end": 1000},
            {"qty": 19.8, "unit": "%", "end": 2000},  # こちらが最新
        ]}
        self.assertAlmostEqual(parse_body_fat_percentage(payload, self.CTX), 19.8)


class TestParseAllWithFixtures(unittest.TestCase):
    """Issue #7 の fixture を使ったスモークテスト。集計値の絶対値は間引きで
    変わるが、型と None/not-None の分布は本番と一致する。"""

    def test_2026_04_22_full_populated(self):
        ctx = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)
        result = parse_all(FIXTURE_ROOT / "2026-04-22", date(2026, 4, 22), ctx)
        self.assertIsInstance(result, DailyHealthData)
        # 4/22 は 12/13 populated (瞑想のみなし)
        self.assertIsNotNone(result.step_count)
        self.assertIsNotNone(result.distance_km)
        self.assertIsNotNone(result.active_energy_kcal)
        self.assertIsNotNone(result.exercise_intensity_score)
        self.assertIsNotNone(result.heart_rate_avg)
        self.assertIsNotNone(result.heart_rate_max)
        self.assertIsNotNone(result.heart_rate_resting)
        self.assertIsNotNone(result.oxygen_saturation)
        self.assertIsNotNone(result.sleep_hours)
        self.assertIsNotNone(result.nap_hours)  # 昼寝あり
        self.assertIsNone(result.mindful_sessions)
        self.assertIsNone(result.mindful_minutes)
        self.assertIsNotNone(result.body_mass_kg)
        self.assertIsNotNone(result.body_fat_percentage)

    def test_2026_04_22_exact_values_for_small_records(self):
        """間引きの影響を受けない (元データが 100 件未満の) フィールドは
        本番値と一致する。

        fixture 再生成で件数前提が崩れた場合を自己診断するため、
        元データの件数そのものも assert する (evenly --limit 100 で全件保持される
        ことが exact 値の根拠)。
        """
        import json as _json
        fixture_dir = FIXTURE_ROOT / "2026-04-22"
        # 件数前提の自己検証: 100 件未満 = 間引きで変化なし
        for name, expected_max in [
            ("sleep_analysis.json", 100),
            ("weight_body_mass.json", 100),
            ("body_fat_percentage.json", 100),
            ("resting_heart_rate.json", 100),
        ]:
            payload = _json.loads((fixture_dir / name).read_text(encoding="utf-8"))
            n = len(payload.get("data", []))
            self.assertLess(
                n, expected_max,
                f"{name}: {n} records >= limit {expected_max}, "
                f"exact 値前提が崩れている (fixture を --limit {n+1}+ で再生成?)",
            )

        ctx = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)
        result = parse_all(fixture_dir, date(2026, 4, 22), ctx)
        # sleep_analysis は 29 records (全件保持)
        self.assertAlmostEqual(result.sleep_hours, 4.0333333333, places=5)
        self.assertAlmostEqual(result.nap_hours, 1.5666666666, places=5)
        # weight_body_mass は 3 records (全件保持)
        self.assertAlmostEqual(result.body_mass_kg, 65.55, places=2)
        # body_fat_percentage は 1 record (全件保持)
        self.assertAlmostEqual(result.body_fat_percentage, 19.8, places=2)
        # heart_rate_resting は 1 record (全件保持)
        self.assertAlmostEqual(result.heart_rate_resting, 64.0, places=2)

    def test_2026_04_23_partial_missing(self):
        ctx = ParseContext(target_date=date(2026, 4, 23), max_heart_rate=178.0)
        result = parse_all(FIXTURE_ROOT / "2026-04-23", date(2026, 4, 23), ctx)
        # 昼寝なし、体重なし、体脂肪なし、瞑想あり
        self.assertIsNone(result.nap_hours)
        self.assertIsNone(result.body_mass_kg)
        self.assertIsNone(result.body_fat_percentage)
        self.assertIsNotNone(result.mindful_sessions)
        self.assertIsNotNone(result.mindful_minutes)
        # 残りは populated
        self.assertIsNotNone(result.step_count)
        self.assertIsNotNone(result.sleep_hours)

    def test_parse_all_missing_file_gracefully(self):
        """アーカイブディレクトリに一部ファイルがない場合も parse_all は
        該当フィールドだけ None で続行する (全滅させない)。"""
        import tempfile
        from shutil import copyfile
        src_dir = FIXTURE_ROOT / "2026-04-22"
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "2026-04-22"
            dst.mkdir()
            # step_count.json だけコピー、他は存在しない
            copyfile(src_dir / "step_count.json", dst / "step_count.json")
            ctx = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)
            result = parse_all(dst, date(2026, 4, 22), ctx)
            self.assertIsNotNone(result.step_count)
            # その他のフィールドは all None
            self.assertIsNone(result.distance_km)
            self.assertIsNone(result.sleep_hours)
            self.assertIsNone(result.body_mass_kg)


if __name__ == "__main__":
    unittest.main()
