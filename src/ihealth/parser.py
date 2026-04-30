"""AutoSync の解凍済み JSON からメトリクス別の日次集計値を取り出す。

source.py が ``data/health/YYYY-MM-DD/{metric_dir}.json`` にアーカイブした
11 ファイルを入力とし、14 フィールド (date + 13) の :class:`DailyHealthData` を返す。

各集計関数は次の規約で統一:
    def parse_<field>(payload: dict, ctx: ParseContext) -> int | float | None

``payload`` は ``{"metric": ..., "data": [...], "date": ...}`` の dict。
``ctx`` は対象日付・最大心拍数など、集計に必要な呼び出し側のコンテキスト。
``data`` が空 or 該当レコードが無い場合は ``None`` を返す。

**集計ルール (実データ検証済み)** の要点:

* ``active_energy`` は ``kcal`` / ``kJ`` の 2 単位を重複出力するので ``kcal`` のみ合計
* ``weight_body_mass`` は ``kg`` / ``lb`` の 2 単位を重複出力するので ``kg`` のみ採用
* ``heart_rate`` は 1 ファイル → 3 フィールドに分解:
  ``heart_rate_avg`` (mean of avg), ``heart_rate_max`` (max of max),
  ``exercise_intensity_score`` (Google Fit Heart Points 相当の自前算出)
* ``mindful_minutes`` は 1 ファイル → ``mindful_sessions`` (count) と
  ``mindful_minutes`` (duration の合計) の 2 フィールドに分解
* ``sleep_analysis`` は AutoSleep と Apple Watch から重複投入される可能性があるので、
  ``sources[*].identifier`` 単位で ``asleep`` の SUM を取り、最大ソースを採用
* ``sleep_analysis`` の ``.hae`` は**開始日基準**で分割されるので、当日ファイルに
  「前夜→当日朝」の睡眠と「当日夜→翌朝」の未完就寝が混在する。``end.hour`` JST で
  切り分けて ``sleep_hours`` (夜主睡眠) と ``nap_hours`` (昼寝) に振り分ける
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Union

from ihealth.models import DailyHealthData


_logger = logging.getLogger(__name__)


# Mac absolute time epoch (2001-01-01 UTC) と JST タイムゾーン。
# HealthKit のタイムスタンプはこの epoch 起点の秒数で格納される。
_MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_JST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class ParseContext:
    """集計関数に共通で流し込む呼び出し元コンテキスト。

    全パース関数が受け取るが、使うかどうかは関数ごと。
    最大心拍数は ``exercise_intensity_score`` の強度ゾーン判定で必須。
    Tanaka 式 (``205.8 − 0.685 × age``) で算出されるため、整数ではなく float。
    """

    target_date: date
    max_heart_rate: float


def _jst_datetime(mac_seconds: float) -> datetime:
    """Mac absolute time (秒) を JST タイムゾーン付き datetime に変換する。"""
    return (_MAC_EPOCH + timedelta(seconds=float(mac_seconds))).astimezone(_JST)


def _jst_hour(mac_seconds: float) -> int:
    """Mac absolute time (秒) を JST の時刻成分 (0-23) に変換する。後方互換。"""
    return _jst_datetime(mac_seconds).hour


def _sum_asleep_by_source(
    payload: "dict",
    target_date: date,
    end_hour_min: int,
    end_hour_max: int,
) -> "float | None":
    """``asleep`` レコードを「**end の JST 日付が ``target_date``**」かつ
    「end の JST 時刻が ``[end_hour_min, end_hour_max)``」で絞り、
    ``sources[0].identifier`` 単位で合計して最大ソースの値を返す。

    **Issue #14 の改修**: 従来は時刻 (0-23 時) だけで判定していたが、前日分の
    ``sleep_analysis.json`` を併読する設計になったため、「前日 22:00 → 当日 06:00」
    の主睡眠と「当日 22:00 → 翌日 06:00」の未完就寝を日付でも区別する必要がある。
    end_at の JST 日付が ``target_date`` と一致するレコードのみ集計対象に入れる。

    AutoSleep と Apple Watch が同一期間のデータを重複投入するため、
    ソース横断の単純 SUM は二重計上になる。「同じ睡眠期間に対する複数計測源の中で
    最も大きな合計を提示した 1 ソース」を採用することで、二重計上を避けつつ
    取りこぼしも最小化する。

    実データでは ``sources`` 配列が常に 1 要素 (AutoSleep 単独、Apple Watch10 単独)
    なので ``sources[0]`` でソース識別子が取れる。複数要素になるケースが将来現れたら
    `identifier` の結合キー化が必要。
    """
    by_source: "dict[str, float]" = {}
    for rec in payload.get("data", []):
        asleep = rec.get("asleep")
        if asleep is None:
            continue
        end_at = rec.get("end")
        if end_at is None:
            continue
        try:
            end_dt = _jst_datetime(end_at)
        except (ValueError, TypeError):
            continue
        # JST 日付が target_date と一致するレコードだけ (前日ファイル併読の誤爆対策)
        if end_dt.date() != target_date:
            continue
        if not (end_hour_min <= end_dt.hour < end_hour_max):
            continue
        src = (rec.get("sources") or [{}])[0].get("identifier", "")
        by_source.setdefault(src, 0.0)
        by_source[src] += float(asleep)
    if not by_source:
        return None
    return max(by_source.values())


# ---------- メトリクス別集計関数 (13 個) ----------
# 各関数は `(payload, ctx)` を受け取る規約で統一 (ctx は不要な関数でも受け取る)。


def parse_step_count(payload: dict, ctx: ParseContext) -> "int | None":
    items = [d["qty"] for d in payload.get("data", []) if d.get("unit") == "count"]
    if not items:
        return None
    return int(round(sum(items)))


def parse_distance_km(payload: dict, ctx: ParseContext) -> "float | None":
    items = [d["qty"] for d in payload.get("data", []) if d.get("unit") == "km"]
    if not items:
        return None
    return float(sum(items))


def parse_active_energy_kcal(payload: dict, ctx: ParseContext) -> "float | None":
    # kJ と kcal が両方入るが、kcal だけ合計する (二重計上回避)
    items = [d["qty"] for d in payload.get("data", []) if d.get("unit") == "kcal"]
    if not items:
        return None
    return float(sum(items))


def parse_exercise_intensity_score(
    payload: dict, ctx: ParseContext
) -> "float | None":
    """HAE の sparse HR サンプルから Heart Points 相当を近似する (1 分 bucket 集約)。

    **実データ上の前提** (Issue #13 で判明):
    Health Auto Export が出力する ``heart_rate.hae`` は「**1 秒瞬間サンプル**」の
    集合で、レコード間には数百秒のギャップがある (1 分粒度の window ではない)。
    そのため「連続時間 × zone 倍率」の比例配分や「隣接結合後の整数分」では
    実データで 0 pt に偏ってしまう。

    **アルゴリズム**: 各サンプルを 1 分 bucket (``int(start // 60)``) に振り分け、
    各 bucket 内の ``avg`` を**平均** (``mean``) して zone 判定:

    * bucket 平均 ≥ max_hr × 0.70: 高強度 → 2 pt
    * max_hr × 0.50 ≤ bucket 平均 < max_hr × 0.70: 中強度 → 1 pt
    * それ未満: 0 pt

    **Google Fit 公式仕様との乖離** (明示):

    * Google Fit は「1 分**連続して** zone 内」が要件だが、本実装は
      「その 1 分のうち **観測サンプルが存在した分だけ** の平均」で判定する。
      HAE の 1 秒瞬間サンプルは間隔が不定 (数秒 〜 数分) で、観測されなかった
      57-59 秒の区間を埋める情報がないため、厳密な「連続 1 分」判定は不可能。
    * そのため、孤立した 1 秒サンプルでもその 1 分ぶんの pt が付与される
      (過大計上寄り)。Google Fit の純正値との比較には使えず、**運動量の相対指標**
      として旧 GoogleFitNotionIntegration の意味論に合わせる目的に限定して使う。
    * 複数ソース (Apple Watch + iPhone) から同 bucket に投入されたサンプルは
      単純平均で集約する。**同値 duplicate は結合される** が、異種値
      (Watch=140, iPhone=80 → mean=110) は平均で zone が変わる弱点がある。

    ``max_hr`` は :class:`ihealth.config.Config.max_heart_rate` が返す値
    (Tanaka 式: ``205.8 − 0.685 × age``) を ``ctx`` 経由で受け取る。

    Args:
        payload: ``heart_rate.json`` の内容 (``{"metric", "data":[...]}``)
        ctx: ``max_heart_rate`` を含むパース文脈

    Returns:
        総 Heart Points 近似 (float、0 pt なら None)。
    """
    max_hr = ctx.max_heart_rate
    t_moderate = max_hr * 0.50
    t_vigorous = max_hr * 0.70

    # bucket_index → (avg の合計, サンプル数)
    buckets: "dict[int, tuple[float, int]]" = {}
    for rec in payload.get("data", []):
        avg = rec.get("avg")
        start = rec.get("start")
        if avg is None or start is None:
            continue
        try:
            avg_f = float(avg)
            start_f = float(start)
        except (TypeError, ValueError):
            continue
        bucket = int(start_f // 60)
        prev_sum, prev_count = buckets.get(bucket, (0.0, 0))
        buckets[bucket] = (prev_sum + avg_f, prev_count + 1)

    if not buckets:
        return None

    total_points = 0
    for avg_sum, count in buckets.values():
        mean_avg = avg_sum / count
        if mean_avg >= t_vigorous:
            total_points += 2
        elif mean_avg >= t_moderate:
            total_points += 1

    return float(total_points) if total_points > 0 else None


def parse_heart_rate_avg(payload: dict, ctx: ParseContext) -> "float | None":
    items = [d["avg"] for d in payload.get("data", []) if "avg" in d]
    if not items:
        return None
    return float(statistics.mean(items))


def parse_heart_rate_max(payload: dict, ctx: ParseContext) -> "float | None":
    items = [d["max"] for d in payload.get("data", []) if "max" in d]
    if not items:
        return None
    return float(max(items))


def parse_heart_rate_resting(payload: dict, ctx: ParseContext) -> "float | None":
    items = [
        d["qty"] for d in payload.get("data", [])
        if d.get("unit") == "count/min" and "qty" in d
    ]
    if not items:
        return None
    return float(statistics.mean(items))


def parse_oxygen_saturation(payload: dict, ctx: ParseContext) -> "float | None":
    items: "list[float]" = []
    for d in payload.get("data", []):
        if d.get("unit") != "%":
            continue
        if "avg" in d:
            items.append(d["avg"])
        elif "qty" in d:
            items.append(d["qty"])
    if not items:
        return None
    return float(statistics.mean(items))


def parse_sleep_hours(payload: dict, ctx: ParseContext) -> "float | None":
    """夜主睡眠: 当日朝 (JST 0-13 時台) に起床した asleep レコードのソース別合計。

    ``payload`` には composition root で **前日 + 当日の sleep_analysis.json** を
    結合したものを渡す想定 (Issue #14)。当日ファイル単体でも動作するが、前日夜
    スタートの主睡眠レコードは前日ファイルにあるため、併読しないと過少になる日がある。
    """
    return _sum_asleep_by_source(payload, ctx.target_date, 0, 14)


def parse_nap_hours(payload: dict, ctx: ParseContext) -> "float | None":
    """昼寝: JST 14-19 時台に終わる asleep レコードのソース別合計。

    ``payload`` は parse_sleep_hours と同じく前日+当日の結合を想定する。昼寝は
    start/end ともに当日になるのが通常だが、境界 (14:00 ちょうど就寝 → 14:xx 起床)
    で前日ファイル側に混入する可能性は残るため同じフィルタで揃える。
    """
    return _sum_asleep_by_source(payload, ctx.target_date, 14, 20)


def parse_mindful_sessions(payload: dict, ctx: ParseContext) -> "int | None":
    data = payload.get("data", [])
    return len(data) if data else None


def parse_mindful_minutes(payload: dict, ctx: ParseContext) -> "float | None":
    data = payload.get("data", [])
    if not data:
        return None
    total_seconds = 0.0
    for rec in data:
        start = rec.get("start")
        end = rec.get("end")
        if start is None or end is None:
            continue
        total_seconds += float(end) - float(start)
    if total_seconds <= 0:
        return None
    return total_seconds / 60.0


def parse_body_mass_kg(payload: dict, ctx: ParseContext) -> "float | None":
    kg_records = [d for d in payload.get("data", []) if d.get("unit") == "kg"]
    if not kg_records:
        return None
    latest = max(kg_records, key=lambda d: d.get("end", 0))
    return float(latest["qty"])


def parse_body_fat_percentage(payload: dict, ctx: ParseContext) -> "float | None":
    pct_records = [d for d in payload.get("data", []) if d.get("unit") == "%"]
    if not pct_records:
        return None
    latest = max(pct_records, key=lambda d: d.get("end", 0))
    return float(latest["qty"])


# ---------- フィールド → (アーカイブファイル名, パース関数) の写像 ----------
#
# 注意: ディレクトリ数は 11 だが、heart_rate は 1 ファイルから 3 フィールド、
# mindful_minutes は 1 ファイルから 2 フィールド、sleep_analysis も 2 フィールド
# 派生するので、このテーブルは 13 行になる。
# apple_exercise_time はアーカイブはされるが、現時点では DailyHealthData 側の
# フィールドに使っていない (運動強度スコアは heart_rate ベースの Heart Points 算出に
# 一本化した。旧 GoogleFitNotionIntegration の「運動強度スコア」の意味論に合わせるため)。

#: パース関数の共通シグネチャ。``payload`` と ``ctx`` を取って int / float / None を返す。
ParserFn = Callable[[dict, "ParseContext"], Optional[Union[int, float]]]

FIELD_SOURCES: "dict[str, tuple[str, ParserFn]]" = {
    "step_count":               ("step_count",               parse_step_count),
    "distance_km":              ("walking_running_distance", parse_distance_km),
    "active_energy_kcal":       ("active_energy",            parse_active_energy_kcal),
    "exercise_intensity_score": ("heart_rate",               parse_exercise_intensity_score),
    "heart_rate_avg":           ("heart_rate",               parse_heart_rate_avg),
    "heart_rate_max":           ("heart_rate",               parse_heart_rate_max),
    "heart_rate_resting":       ("resting_heart_rate",       parse_heart_rate_resting),
    "oxygen_saturation":        ("blood_oxygen_saturation",  parse_oxygen_saturation),
    "sleep_hours":              ("sleep_analysis",           parse_sleep_hours),
    "nap_hours":                ("sleep_analysis",           parse_nap_hours),
    "mindful_sessions":         ("mindful_minutes",          parse_mindful_sessions),
    "mindful_minutes":          ("mindful_minutes",          parse_mindful_minutes),
    "body_mass_kg":             ("weight_body_mass",         parse_body_mass_kg),
    "body_fat_percentage":      ("body_fat_percentage",      parse_body_fat_percentage),
}


def parse_all(
    archive_dir: Path,
    target_date: date,
    ctx: ParseContext,
    payload_overrides: "dict[str, dict] | None" = None,
) -> DailyHealthData:
    """``archive_dir`` 配下のアーカイブ JSON を読み込み、13 フィールドに集計して返す。

    ファイルが存在しない / パース関数が例外を吐いた場合は該当フィールドを ``None``
    にして続行する（1 メトリクスの欠落で全滅しない設計）。

    ``payload_overrides`` に ``{metric_dir: payload_dict}`` を渡すと、該当 metric_dir
    の JSON 読み込みをスキップしてその payload を直接使う。**Issue #14**:
    composition root が前日+当日の ``sleep_analysis`` を結合した payload を
    ``{"sleep_analysis": merged}`` の形で渡すことで、前日夜〜当日朝の主睡眠を
    正しく拾う。parser 自体はディレクトリレイアウト (前日 archive の所在)
    を知らないまま処理できる (レイヤ漏れ回避)。
    """
    # 同じファイルを複数フィールドが参照する (heart_rate, mindful_minutes,
    # sleep_analysis) ので file-level でキャッシュしてディスク読みを減らす
    payload_cache: "dict[str, dict]" = dict(payload_overrides) if payload_overrides else {}
    values: "dict[str, object]" = {}

    for field_name, (dir_name, func) in FIELD_SOURCES.items():
        json_path = archive_dir / f"{dir_name}.json"
        if dir_name not in payload_cache:
            if not json_path.exists():
                _logger.warning(
                    "archive file が見つかりません: %s (field=%s)",
                    json_path, field_name,
                )
                payload_cache[dir_name] = {}
            else:
                try:
                    payload_cache[dir_name] = json.loads(
                        json_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError) as exc:
                    _logger.error(
                        "archive file のパースに失敗: %s (field=%s): %s",
                        json_path, field_name, exc,
                    )
                    payload_cache[dir_name] = {}

        payload = payload_cache[dir_name]
        try:
            values[field_name] = func(payload, ctx)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            # データ異常は欠損値扱いで続行 (1 メトリクスの異常で全滅させない)
            _logger.warning(
                "field %s の集計に失敗しました (file=%s): %s",
                field_name, json_path.name, exc,
            )
            values[field_name] = None
        except Exception:
            # 想定外の例外 (実装バグ、ライブラリの挙動変化など) は traceback を残して
            # 呼び出し側に伝播。黙って None にすると回帰や typo を埋もれさせる。
            _logger.exception(
                "field %s の集計で想定外エラー (file=%s)",
                field_name, json_path.name,
            )
            raise

    # DailyHealthData の __post_init__ は各フィールドの生理学的妥当性を検証し、
    # 違反があれば ValueError を raise する (Issue #15)。ここで catch して
    # 該当フィールドを 1 つずつ None に落として再試行することで、1 メトリクスの
    # 異常値 (parser のバグ / センサー誤計測) が全フィールドを道連れにしないようにする。
    try:
        return DailyHealthData(date=target_date, **values)  # type: ignore[arg-type]
    except ValueError as exc:
        _logger.warning(
            "DailyHealthData 妥当性検証に失敗、異常フィールドを None に落として再構築: %s",
            exc,
        )
        return _rebuild_with_validation_drop(target_date, values)


def _rebuild_with_validation_drop(
    target_date: date, values: "dict[str, object]"
) -> DailyHealthData:
    """個別フィールドを試行的に None に落として妥当性検証を通す。

    1 フィールドずつ順に None に置き換えて再構築を試み、成功した時点で返す。
    ``exercise_intensity_score`` → ``heart_rate_avg`` のように依存関係を考慮して
    「怪しい順」に試すのではなく、素朴に dict の順で試す (最悪ケースでも
    14 回の再構築で完了)。全部 None に落としても失敗する場合は諦めて空の
    DailyHealthData を返す (date だけは保持)。
    """
    tentative = dict(values)
    for name in list(tentative.keys()):
        if tentative[name] is None:
            continue
        saved = tentative[name]
        tentative[name] = None
        try:
            return DailyHealthData(date=target_date, **tentative)  # type: ignore[arg-type]
        except ValueError:
            # まだ別フィールドに違反があるので、このフィールドも None のまま次へ
            _logger.warning(
                "field %s を妥当性違反として drop (元値=%r)", name, saved,
            )
            continue
    # 全部 None でも構築できない = date 以外の制約違反 (理論上あり得ない)
    return DailyHealthData(date=target_date)
