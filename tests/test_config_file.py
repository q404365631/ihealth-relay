"""config.toml 読み込み (ihealth.config_file) の回帰テスト.

Phase 1 B1 / Issue #18 で導入。config.toml が:
- 不在の場合は ``find_config_path`` が None を返し、override 空 dict
- 存在する場合は ``[publishers.notion.fields.*]`` から override map を抽出
を保証する。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ihealth.config_file import (
    ConfigFileError,
    extract_notion_date_property,
    extract_notion_property_overrides,
    find_config_path,
    load_config_toml,
)


class TestFindConfigPath(unittest.TestCase):
    def test_returns_none_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(find_config_path(Path(tmp)))

    def test_returns_path_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            p.write_text("", encoding="utf-8")
            self.assertEqual(find_config_path(Path(tmp)), p)

    def test_returns_none_when_directory_not_file(self):
        # config.toml が万が一ディレクトリだったら無視 (.is_file() で False)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.toml").mkdir()
            self.assertIsNone(find_config_path(Path(tmp)))


class TestLoadConfigToml(unittest.TestCase):
    def test_none_path_returns_empty(self):
        self.assertEqual(load_config_toml(None), {})

    def test_loads_valid_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            p.write_text(
                '[publishers.notion.fields.step_count]\nproperty = "歩数 (歩)"\n',
                encoding="utf-8",
            )
            data = load_config_toml(p)
        self.assertEqual(
            data["publishers"]["notion"]["fields"]["step_count"]["property"],
            "歩数 (歩)",
        )


class TestExtractNotionPropertyOverrides(unittest.TestCase):
    def test_extracts_simple_map(self):
        config = {
            "publishers": {
                "notion": {
                    "fields": {
                        "step_count": {"property": "歩数 (歩)"},
                        "distance_km": {"property": "移動距離 (km)"},
                    }
                }
            }
        }
        result = extract_notion_property_overrides(config)
        self.assertEqual(
            result,
            {"step_count": "歩数 (歩)", "distance_km": "移動距離 (km)"},
        )

    def test_returns_empty_when_publishers_missing(self):
        self.assertEqual(extract_notion_property_overrides({}), {})

    def test_returns_empty_when_notion_missing(self):
        self.assertEqual(extract_notion_property_overrides({"publishers": {}}), {})

    def test_returns_empty_when_fields_missing(self):
        self.assertEqual(
            extract_notion_property_overrides({"publishers": {"notion": {}}}),
            {},
        )

    def test_raises_on_non_dict_field_entry(self):
        """fields.<field> が table でない場合は fail-fast (codex 指摘 2026-04-29)."""
        config = {
            "publishers": {"notion": {"fields": {"step_count": "not a table"}}}
        }
        with self.assertRaises(ConfigFileError) as ctx:
            extract_notion_property_overrides(config)
        self.assertIn("step_count", str(ctx.exception))

    def test_skips_entries_without_property_key(self):
        """property キーが無いだけなら既定値が使われる (= silent skip で OK)."""
        config = {
            "publishers": {
                "notion": {
                    "fields": {
                        "step_count": {"converter": "int"},  # property なし
                        "distance_km": {"property": "移動距離 (km)"},
                    }
                }
            }
        }
        self.assertEqual(
            extract_notion_property_overrides(config),
            {"distance_km": "移動距離 (km)"},
        )

    def test_raises_on_non_string_property(self):
        """property が文字列でないユーザーミスは fail-fast (silent data loss 防止)."""
        config = {
            "publishers": {"notion": {"fields": {"step_count": {"property": 123}}}}
        }
        with self.assertRaises(ConfigFileError) as ctx:
            extract_notion_property_overrides(config)
        self.assertIn("step_count", str(ctx.exception))
        self.assertIn("property", str(ctx.exception))

    def test_raises_on_empty_property(self):
        config = {
            "publishers": {"notion": {"fields": {"step_count": {"property": ""}}}}
        }
        with self.assertRaises(ConfigFileError):
            extract_notion_property_overrides(config)

    def test_strict_mode_raises_on_unknown_field(self):
        """valid_field_names を渡した場合、未知の field 名で raise (typo 検出)."""
        config = {
            "publishers": {
                "notion": {
                    "fields": {
                        "step_count": {"property": "歩数"},
                        "step_count_typo": {"property": "歩数"},  # typo
                    }
                }
            }
        }
        with self.assertRaises(ConfigFileError) as ctx:
            extract_notion_property_overrides(
                config, valid_field_names=frozenset({"step_count", "distance_km"}),
            )
        self.assertIn("step_count_typo", str(ctx.exception))

    def test_strict_mode_passes_with_valid_fields(self):
        config = {
            "publishers": {
                "notion": {
                    "fields": {
                        "step_count": {"property": "歩数"},
                    }
                }
            }
        }
        result = extract_notion_property_overrides(
            config, valid_field_names=frozenset({"step_count", "distance_km"}),
        )
        self.assertEqual(result, {"step_count": "歩数"})

    def test_raises_on_fields_not_a_table(self):
        config = {"publishers": {"notion": {"fields": "broken"}}}
        with self.assertRaises(ConfigFileError):
            extract_notion_property_overrides(config)

    def test_strict_raises_on_publishers_not_a_table(self):
        """codex round 2 指摘: publishers = "oops" を silent ignore していた"""
        config = {"publishers": "oops"}
        with self.assertRaises(ConfigFileError):
            extract_notion_property_overrides(
                config, valid_field_names=frozenset({"step_count"}),
            )

    def test_strict_raises_on_notion_not_a_table(self):
        """codex round 2 指摘: publishers.notion = "oops" を silent ignore していた"""
        config = {"publishers": {"notion": "oops"}}
        with self.assertRaises(ConfigFileError):
            extract_notion_property_overrides(
                config, valid_field_names=frozenset({"step_count"}),
            )

    def test_non_strict_silent_skip_on_publishers_string(self):
        """fail-open API として呼ばれた場合は後方互換で silent skip (空 dict)."""
        config = {"publishers": "oops"}
        # valid_field_names=None なら strict=False
        self.assertEqual(extract_notion_property_overrides(config), {})


class TestExtractNotionDateProperty(unittest.TestCase):
    """Phase 1 B2: ``[publishers.notion] date_property`` の override."""

    def test_returns_none_when_unset(self):
        self.assertIsNone(extract_notion_date_property({}))
        self.assertIsNone(extract_notion_date_property({"publishers": {}}))
        self.assertIsNone(extract_notion_date_property({"publishers": {"notion": {}}}))

    def test_returns_value_when_set(self):
        config = {"publishers": {"notion": {"date_property": "日付"}}}
        self.assertEqual(extract_notion_date_property(config), "日付")

    def test_raises_on_non_string(self):
        config = {"publishers": {"notion": {"date_property": 123}}}
        with self.assertRaises(ConfigFileError):
            extract_notion_date_property(config)

    def test_raises_on_empty_string(self):
        config = {"publishers": {"notion": {"date_property": ""}}}
        with self.assertRaises(ConfigFileError):
            extract_notion_date_property(config)

    def test_raises_on_publishers_not_table(self):
        config = {"publishers": "oops"}
        with self.assertRaises(ConfigFileError):
            extract_notion_date_property(config)

    def test_raises_on_notion_not_table(self):
        config = {"publishers": {"notion": "oops"}}
        with self.assertRaises(ConfigFileError):
            extract_notion_date_property(config)


if __name__ == "__main__":
    unittest.main()
