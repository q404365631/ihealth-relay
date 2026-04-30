"""publisher (Notion / Markdown / stdout / SQLite / Slack) に書き込む 1 日分の集計済みヘルスデータ。

13 フィールドすべて ``Optional`` で、データ欠落は ``None`` で表現する
(その日は測っていない / センサー断 / iPhone 同期失敗 などを区別しない)。

## None 扱いの方針 (Issue #15)

**書き込み時に None は "送信しない / 既存値を保持"** が原則。各 publisher が
None フィールドをスキップする (Notion: ページのプロパティを保持, SQLite:
``COALESCE`` で既存値保持, Markdown/stdout: フィールドそのものを出力しない)。

これは「測り忘れた日を手動で埋めた値が次の実行で勝手に空になる」事故を防ぐため。
「今日は敢えて測らなかった」ことを sink 側に記録したい場合は publisher 固有の
方法 (Notion なら UI で手動削除) で対応する運用。

## 妥当性検証 (Issue #15)

各フィールドに生理学的に妥当な範囲の事前検証を入れて、parser の集計バグで
異常値 (負の歩数、0 bpm、150% 体脂肪率など) が Notion を汚染するのを防ぐ。
検証違反は ``ValueError`` を raise し、parser 側の try/except で当該フィールドだけ
None に丸められて続行する想定。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields as dataclass_fields
from datetime import date
from typing import Iterator


#: 各フィールドの妥当範囲 (inclusive lo, inclusive hi)。
#: ``None`` の場合は「0 以上」とだけ検証する (歩数や時間など上限が曖昧なもの)。
_VALID_RANGES: "dict[str, tuple[float | None, float | None]]" = {
    "step_count":               (0, None),
    "distance_km":              (0, 500),      # 1 日 500km 以上歩く人類は希
    "active_energy_kcal":       (0, 20000),    # エリートアスリートでも 1 日 1 万 kcal 未満
    "exercise_intensity_score": (0, None),     # pt 上限なし (Issue #13 の 1 分 bucket 集約)
    "heart_rate_avg":           (20, 250),     # 成人心拍の生理学的範囲
    "heart_rate_max":           (20, 250),
    "heart_rate_resting":       (20, 150),     # 安静時は 150 超えない
    "oxygen_saturation":        (50, 100),
    "sleep_hours":              (0, 24),
    "nap_hours":                (0, 24),
    "mindful_sessions":         (0, None),
    "mindful_minutes":          (0, 24 * 60),  # 1 日分が上限
    "body_mass_kg":             (10, 300),
    "body_fat_percentage":      (1, 60),
}


@dataclass(frozen=True)
class DailyHealthData:
    """1 日分の集計値。値は parser が返す生の SI 単位系 (kg, km, kcal, bpm, hours,
    minutes, counts, percent) のまま保持する。Notion 向けの丸め (小数 1 桁、
    時間→分変換など) は notion.py の書き込み層で行う (parser / models は集計と
    型だけに責務を絞る)。"""

    date: date
    step_count: "int | None" = None
    distance_km: "float | None" = None
    active_energy_kcal: "float | None" = None
    exercise_intensity_score: "float | None" = None
    heart_rate_avg: "float | None" = None
    heart_rate_max: "float | None" = None
    heart_rate_resting: "float | None" = None
    oxygen_saturation: "float | None" = None
    sleep_hours: "float | None" = None
    nap_hours: "float | None" = None
    mindful_sessions: "int | None" = None
    mindful_minutes: "float | None" = None
    body_mass_kg: "float | None" = None
    body_fat_percentage: "float | None" = None

    def __post_init__(self) -> None:
        """各フィールドの値が妥当範囲にあるか検証する。

        検証違反は :class:`ValueError` を raise する。parser.py の ``parse_all`` で
        個別フィールド単位の ``ValueError`` は catch して該当フィールドだけ
        ``None`` にリセットして続行する想定 (全滅を防ぐ)。
        """
        for name, (lo, hi) in _VALID_RANGES.items():
            value = getattr(self, name)
            if value is None:
                continue
            if lo is not None and value < lo:
                raise ValueError(
                    f"{name} が下限 {lo} を下回っています: {value}"
                )
            if hi is not None and value > hi:
                raise ValueError(
                    f"{name} が上限 {hi} を超えています: {value}"
                )
        # heart_rate_avg <= heart_rate_max の物理的整合性
        if (
            self.heart_rate_avg is not None
            and self.heart_rate_max is not None
            and self.heart_rate_avg > self.heart_rate_max
        ):
            raise ValueError(
                f"heart_rate_avg ({self.heart_rate_avg}) が "
                f"heart_rate_max ({self.heart_rate_max}) を超えています"
            )

    def iter_fields(self) -> "Iterator[tuple[str, object]]":
        """``(field_name, value)`` を 14 フィールドぶん順に yield する。

        ``date`` 以外は Python 型 (int / float / None) をそのまま返す。
        ログ出力 / テスト / デバッグで使う。Notion 書き込み向けの変換は
        :func:`ihealth.publishers.notion.build_properties` の責務。
        """
        for f in dataclass_fields(self):
            yield f.name, getattr(self, f.name)

    def as_dict(self) -> "dict[str, object]":
        """すべてのフィールドを素の dict に変換する (ログ / デバッグ用)。

        ``None`` のフィールドも含めて返す (欠落状況を confirm できるように)。
        **Notion への送信ペイロード組み立てには使わない** -- それは
        :func:`ihealth.publishers.notion.build_properties` の責務で、
        ``None`` は「送信しない = 既存値を保持」として扱う
        (空欄にクリアしたいときは Notion 側で直接消す運用)。
        """
        return {
            "date": self.date.isoformat(),
            "step_count": self.step_count,
            "distance_km": self.distance_km,
            "active_energy_kcal": self.active_energy_kcal,
            "exercise_intensity_score": self.exercise_intensity_score,
            "heart_rate_avg": self.heart_rate_avg,
            "heart_rate_max": self.heart_rate_max,
            "heart_rate_resting": self.heart_rate_resting,
            "oxygen_saturation": self.oxygen_saturation,
            "sleep_hours": self.sleep_hours,
            "nap_hours": self.nap_hours,
            "mindful_sessions": self.mindful_sessions,
            "mindful_minutes": self.mindful_minutes,
            "body_mass_kg": self.body_mass_kg,
            "body_fat_percentage": self.body_fat_percentage,
        }
