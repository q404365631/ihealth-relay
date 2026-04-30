"""``config.toml`` 読み込み (Phase 1 B1 / Issue #18).

OSS 公開向けのユーザー固有設定を ``.env`` (秘密情報) と分離し、構造化された
形式で外部化する。`.env` には ``NOTION_SECRET`` ``DATABASE_ID`` などの
**秘密値だけ** を残し、Notion DB のプロパティ名マッピングなどの **構造的設定**
は本ファイルから読み出す ``config.toml`` に書く。

設計方針 (Phase 1 B1 のスコープ):
    - **後方互換最優先**: ``config.toml`` 不在時は内部 DEFAULT_PROPERTY_MAP
      (``publishers/notion.py``) がそのまま使われる = 既存 launchd 運用は無修正で継続
    - **段階的移行**: B1 では Notion ``property`` 名 (= Notion DB 列名) の
      override のみ対応。converter (単位変換) ロジックの差し替え、複数 Sink の
      enabled/disabled 切り替えは Phase 2 以降
    - **TOML 最小 subset**: 自前 :mod:`ihealth._toml` で読み込む (外部依存ゼロ)

設定ファイル形式 (例)::

    # config.toml (リポジトリルートに配置、.gitignore 対象)

    [publishers.notion.fields.step_count]
    property = "歩数 (歩)"

    [publishers.notion.fields.distance_km]
    property = "移動距離 (km)"

    # ... (他 11 フィールド)

参照優先順位:
    1. ``config.toml`` の ``[publishers.notion.fields.<field>]`` の ``property``
    2. (未指定なら) :data:`ihealth.publishers.notion.PROPERTY_MAP` の既定値

旧 ``歩数 (歩)`` 等の日本語名は B1 段階では DEFAULT_PROPERTY_MAP に残す。
B2 (= Phase 1 後半 PR) で英語キーに切り替え予定。それまで既存運用は無修正。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ihealth import _toml


_logger = logging.getLogger(__name__)


def find_config_path(project_root: Path) -> "Path | None":
    """プロジェクトルートに ``config.toml`` が存在すれば返す。なければ ``None``.

    探索場所:
        1. ``$IHEALTH_CONFIG_PATH`` (環境変数で明示指定された場合) — 後の Phase
        2. ``<project_root>/config.toml``

    現時点では (2) のみ。環境変数経路は Phase 2 で導入。
    """
    candidate = project_root / "config.toml"
    return candidate if candidate.is_file() else None


class ConfigFileError(ValueError):
    """``config.toml`` の構造的エラー (unknown field / non-string property など)。

    TOML 構文エラー (:class:`TomlParseError`) より上位レイヤの「論理的に正しくない」
    エラーを表す。上位の ``__main__`` で ConfigAppError に変換される。
    """


def load_config_toml(path: "Path | None") -> "dict[str, Any]":
    """``path`` の TOML を読み込んで dict を返す。``path=None`` なら空 dict。

    Args:
        path: ``find_config_path()`` の戻り値、または明示パス。

    Raises:
        TomlParseError: 構文エラー (上位の ``__main__`` で ConfigAppError 化)。
        OSError: ファイルアクセス失敗 (上位で同様に変換)。

    Returns:
        パース済みの dict。``path is None`` のときは空 dict (= override 無し)。
    """
    if path is None:
        _logger.debug("config.toml が見つかりません (= 既定 PROPERTY_MAP を使用)")
        return {}
    # BOM 付き UTF-8 (Windows 系エディタが付与しがち) も読めるよう ``utf-8-sig``
    text = path.read_text(encoding="utf-8-sig")
    data = _toml.parse(text)
    _logger.info("config.toml をロードしました: %s", path)
    return data


def extract_notion_property_overrides(
    config_data: "dict[str, Any]",
    *,
    valid_field_names: "frozenset[str] | None" = None,
) -> "dict[str, str]":
    """``config.toml`` から ``[publishers.notion.fields.<field>].property`` を抽出する。

    返り値は ``{field_name: notion_property_name}`` の dict。例えば::

        config.toml:
            [publishers.notion.fields.step_count]
            property = "歩数 (歩)"

        → {"step_count": "歩数 (歩)"}

    Args:
        config_data: :func:`load_config_toml` の戻り値
        valid_field_names: 有効な :class:`DailyHealthData` フィールド名の集合.
            指定すると、ここに含まれない field 名 (typo, unknown) があれば
            :class:`ConfigFileError` を raise する (strict mode). 省略時は
            unknown を silent に無視する (= 後方互換のフォールバック).

    Raises:
        ConfigFileError: ``[publishers.notion.fields]`` 配下の構造が不正な場合 —
            unknown field 名, ``property`` が文字列でない, ``property`` が空文字列,
            entry が table でない, など. ``valid_field_names`` を渡したときのみ
            strict 検証する.

    Returns:
        フィールドごとの override map. 設定されていないフィールドは含まない
        (= 内部 DEFAULT_PROPERTY_MAP の値が使われる).
    """
    strict = valid_field_names is not None
    publishers = config_data.get("publishers")
    if publishers is None:
        return {}
    if not isinstance(publishers, dict):
        if strict:
            raise ConfigFileError(
                f"config.toml の 'publishers' は table である必要があります "
                f"(got {type(publishers).__name__})"
            )
        return {}
    notion = publishers.get("notion")
    if notion is None:
        return {}
    if not isinstance(notion, dict):
        if strict:
            raise ConfigFileError(
                f"config.toml の 'publishers.notion' は table である必要があります "
                f"(got {type(notion).__name__})"
            )
        return {}
    fields = notion.get("fields")
    if fields is None:
        return {}
    if not isinstance(fields, dict):
        raise ConfigFileError(
            f"config.toml の 'publishers.notion.fields' は table である必要があります "
            f"(got {type(fields).__name__})"
        )

    overrides: "dict[str, str]" = {}
    for field_name, entry in fields.items():
        if valid_field_names is not None and field_name not in valid_field_names:
            raise ConfigFileError(
                f"config.toml: 未知の field 名 {field_name!r} "
                f"(有効: {sorted(valid_field_names)})"
            )
        if not isinstance(entry, dict):
            raise ConfigFileError(
                f"config.toml: [publishers.notion.fields.{field_name}] は "
                f"table である必要があります (got {type(entry).__name__})"
            )
        prop = entry.get("property")
        if prop is None:
            # property キーが無いだけなら silent skip (= 既定値が使われる)
            continue
        if not isinstance(prop, str):
            raise ConfigFileError(
                f"config.toml: [publishers.notion.fields.{field_name}].property "
                f"は文字列である必要があります (got {type(prop).__name__})"
            )
        if not prop:
            raise ConfigFileError(
                f"config.toml: [publishers.notion.fields.{field_name}].property "
                "が空文字列です"
            )
        overrides[field_name] = prop
    return overrides


def extract_notion_date_property(
    config_data: "dict[str, Any]",
) -> "str | None":
    """``config.toml`` から ``[publishers.notion] date_property`` を抽出する.

    Phase 1 B2 で導入. 別名の Notion DB の "日付" 列名を override するため.
    例::

        [publishers.notion]
        date_property = "日付"

    Args:
        config_data: :func:`load_config_toml` の戻り値.

    Raises:
        ConfigFileError: 構造不正 / 値が文字列でない / 空文字列.

    Returns:
        指定されていれば文字列、未指定なら ``None``
        (= ``NotionClient.DEFAULT_DATE_PROPERTY_NAME`` が使われる).
    """
    publishers = config_data.get("publishers")
    if publishers is None:
        return None
    if not isinstance(publishers, dict):
        raise ConfigFileError(
            f"config.toml の 'publishers' は table である必要があります "
            f"(got {type(publishers).__name__})"
        )
    notion = publishers.get("notion")
    if notion is None:
        return None
    if not isinstance(notion, dict):
        raise ConfigFileError(
            f"config.toml の 'publishers.notion' は table である必要があります "
            f"(got {type(notion).__name__})"
        )
    value = notion.get("date_property")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigFileError(
            f"config.toml: [publishers.notion].date_property は文字列である "
            f"必要があります (got {type(value).__name__})"
        )
    if not value.strip():
        raise ConfigFileError(
            "config.toml: [publishers.notion].date_property が空 / 空白のみです"
        )
    return value
