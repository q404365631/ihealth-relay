"""Publisher 共通の payload 検証 / coercion ロジック (Phase 1 A3-stdout で導入).

Markdown / stdout / SQLite / Slack 等の各 frontend は、表現フォーマット (YAML
frontmatter / JSON line / SQL row / Slack blocks) は異なるが、内部で持つべき
**型と精度** は同じ:

- ``step_count`` は **正確に ``int``** (``True`` / ``1.9`` を silent に通さない)
- ``distance_km`` は ``float`` で 2 桁丸め
- ``oxygen_saturation`` は ``float`` で 2 桁
- ...

このモジュールは「``DailyHealthData`` を **検証・coerce 済みの flat dict** に
落とす」ところまでを共通化する. 出力フォーマットへの整形 (``json.dumps`` /
``str(value)`` / SQL parameter binding) は呼び出し側 publisher の責務.

このモジュールは publisher 間でのみ使う private API なので、``ihealth.publishers``
パッケージ外からの import は想定しない (アンダースコア prefix で明示).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields as dataclass_fields
from types import MappingProxyType
from typing import Mapping

from ihealth.models import DailyHealthData


@dataclass(frozen=True)
class _FieldSpec:
    """``DailyHealthData`` のフィールドを共通 dict に落とすときの仕様.

    - ``kind="int"``: 値は **正確に ``int``** であることを要求 (``bool`` は
      Python では ``int`` の subclass だが「step_count=True」のような parser
      バグを silent に通すと混乱するため fail-fast で弾く).
    - ``kind="float"``: ``int`` / ``float`` のみ受け付け、``ndigits`` で
      ``round`` する. ``bool`` は明示的に reject. NaN/Inf も reject.
    """

    kind: str
    ndigits: "int | None"


#: ``DailyHealthData`` のフィールド名 → 共通 :class:`_FieldSpec`.
#:
#: 単位は ``DailyHealthData`` そのまま (hours / km / kg / kcal / bpm / %).
#: Notion publisher だけが 別名の Notion DB 互換のため別途分換算するが、
#: それ以外 (Markdown / stdout / SQLite / Slack) はこの spec をそのまま使う.
#:
#: が本 dict の alias として export されていたため、外部から mutate されると
#: 全 publisher の payload 仕様が壊れる shared mutable state リスクがあった.
#: :class:`types.MappingProxyType` で read-only view に固定する.
_FIELD_SPECS_RAW: "dict[str, _FieldSpec]" = {
    "step_count":               _FieldSpec(kind="int",   ndigits=None),
    "distance_km":              _FieldSpec(kind="float", ndigits=2),
    "active_energy_kcal":       _FieldSpec(kind="float", ndigits=1),
    "exercise_intensity_score": _FieldSpec(kind="float", ndigits=1),
    "heart_rate_avg":           _FieldSpec(kind="float", ndigits=1),
    "heart_rate_max":           _FieldSpec(kind="float", ndigits=1),
    "heart_rate_resting":       _FieldSpec(kind="float", ndigits=1),
    "oxygen_saturation":        _FieldSpec(kind="float", ndigits=2),
    "sleep_hours":              _FieldSpec(kind="float", ndigits=2),
    "nap_hours":                _FieldSpec(kind="float", ndigits=2),
    "mindful_sessions":         _FieldSpec(kind="int",   ndigits=None),
    "mindful_minutes":          _FieldSpec(kind="float", ndigits=1),
    "body_mass_kg":             _FieldSpec(kind="float", ndigits=1),
    "body_fat_percentage":      _FieldSpec(kind="float", ndigits=1),
}

#: 公開された FIELD_SPECS (read-only). ``[name]`` lookup と ``in`` / iteration
#: ``len()`` ``items()`` は通常の dict と同じく動くが、要素追加 / 削除は
#: ``TypeError: 'mappingproxy' object does not support item assignment`` で弾かれる.
FIELD_SPECS: "Mapping[str, _FieldSpec]" = MappingProxyType(_FIELD_SPECS_RAW)


class PayloadValidationError(RuntimeError):
    """``DailyHealthData`` のフィールド検証で発生する例外.

    各 publisher 側はこれを catch して publisher 固有のエラー型 (例:
    :class:`MarkdownPublishError`) にラップして再 raise する.
    """


def _validate_field_specs() -> None:
    """``FIELD_SPECS`` が ``DailyHealthData`` と 1:1 で一致することを保証する.

    モジュール import 時に 1 回実行する sanity check.
    spec.kind の domain (``"int"`` / ``"float"``) と ndigits の整合性も検証.
    """
    model_fields = {f.name for f in dataclass_fields(DailyHealthData)} - {"date"}
    spec_fields = set(_FIELD_SPECS_RAW.keys())
    only_in_model = model_fields - spec_fields
    only_in_specs = spec_fields - model_fields
    if only_in_model or only_in_specs:
        raise RuntimeError(
            "FIELD_SPECS と DailyHealthData のフィールドが一致しません: "
            f"only_in_model={sorted(only_in_model)}, "
            f"only_in_specs={sorted(only_in_specs)}"
        )
    for name, spec in _FIELD_SPECS_RAW.items():
        if spec.kind not in ("int", "float"):
            raise RuntimeError(
                f"FIELD_SPECS[{name!r}].kind が不正: {spec.kind!r}"
            )
        if spec.kind == "int" and spec.ndigits is not None:
            raise RuntimeError(
                f"FIELD_SPECS[{name!r}]: kind=int では ndigits=None"
            )
        if spec.kind == "float" and spec.ndigits is None:
            raise RuntimeError(
                f"FIELD_SPECS[{name!r}]: kind=float では ndigits 必須"
            )


_validate_field_specs()


def coerce_int(field_name: str, raw: object) -> int:
    """``raw`` を strict に ``int`` に検証する.

    - ``type(raw) is int`` で **bool / float / str / その他をすべて reject**.
      ``bool`` は ``isinstance(raw, int)`` で True になるため、``type()`` 比較が必要.
    """
    if type(raw) is not int:  # noqa: E721 — int の strict 比較が意図
        raise PayloadValidationError(
            f"フィールド {field_name!r} は int でなければなりません: "
            f"値={raw!r} 型={type(raw).__name__}"
        )
    return raw


def coerce_float(field_name: str, raw: object, ndigits: int) -> float:
    """``raw`` を ``float`` に変換し ``ndigits`` 桁で round する.

    - ``bool`` は明示的に reject.
    - ``int`` は ``float`` への明示変換を許可.
    - ``float`` も許可. ``str`` その他は reject.
    - ``NaN`` / ±``Inf`` は reject (出力先によっては parse 不能になる).
    """
    if isinstance(raw, bool):
        raise PayloadValidationError(
            f"フィールド {field_name!r} は数値でなければなりません: bool 不可 "
            f"(値={raw!r})"
        )
    if not isinstance(raw, (int, float)):
        raise PayloadValidationError(
            f"フィールド {field_name!r} は数値 (int/float) でなければなりません: "
            f"値={raw!r} 型={type(raw).__name__}"
        )
    value = float(raw)
    if not math.isfinite(value):
        raise PayloadValidationError(
            f"フィールド {field_name!r} に NaN/Inf は出力できません: 値={value!r}"
        )
    return round(value, ndigits)


def convert_field(field_name: str, raw: object, spec: _FieldSpec) -> object:
    """``spec.kind`` に応じて :func:`coerce_int` / :func:`coerce_float` を呼ぶ."""
    if spec.kind == "int":
        return coerce_int(field_name, raw)
    if spec.kind == "float":
        # _validate_field_specs で kind=float なら ndigits is not None が保証
        assert spec.ndigits is not None  # for type-checker
        return coerce_float(field_name, raw, spec.ndigits)
    raise RuntimeError(f"unknown spec.kind: {spec.kind!r}")  # pragma: no cover


def build_payload(health_data: DailyHealthData) -> "dict[str, object]":
    """``DailyHealthData`` を **検証 + coerce 済み** flat dict に変換する.

    - ``date`` フィールドは必ず先頭に ISO 文字列として入る.
    - 他のフィールドは :data:`FIELD_SPECS` の iteration 順 (定義順) で出力.
      ``None`` のフィールドは省略 (Notion publisher と同じセマンティクス).
    - 値は :func:`convert_field` で型・精度を強制. silent coercion なし.

    Raises:
        PayloadValidationError: いずれかのフィールドで型違反 / NaN / Inf 等.

    Returns:
        ``{"date": "2026-04-22", "step_count": 12345, ...}`` のような dict.
        この戻り値を JSON / YAML / SQL row のいずれにも安全に変換できる.
    """
    payload: "dict[str, object]" = {"date": health_data.date.isoformat()}
    for field_name, spec in FIELD_SPECS.items():
        raw = getattr(health_data, field_name, None)
        if raw is None:
            continue
        payload[field_name] = convert_field(field_name, raw, spec)
    return payload
