"""自前 TOML 最小パーサ (ihealth._toml) の回帰テスト.

Phase 1 B1 / Issue #18 で導入。本プロジェクトの config.toml に必要な
subset (string, int, list, table 入れ子, コメント) を保証する。
"""

from __future__ import annotations

import unittest

from ihealth._toml import TomlParseError, parse


class TestParseBasics(unittest.TestCase):
    """key = value の基本ケース。"""

    def test_basic_string(self):
        self.assertEqual(parse('key = "hello"'), {"key": "hello"})

    def test_literal_string_no_escape(self):
        # literal string は escape 解釈しない
        self.assertEqual(parse(r"key = 'a\nb'"), {"key": r"a\nb"})

    def test_basic_string_unescape(self):
        self.assertEqual(parse(r'key = "a\nb"'), {"key": "a\nb"})
        self.assertEqual(parse(r'key = "tab\there"'), {"key": "tab\there"})
        self.assertEqual(parse(r'key = "quote\"inside"'), {"key": 'quote"inside'})

    def test_integer(self):
        self.assertEqual(parse("count = 42"), {"count": 42})

    def test_negative_integer(self):
        self.assertEqual(parse("delta = -7"), {"delta": -7})

    def test_string_list(self):
        result = parse('items = ["a", "b", "c"]')
        self.assertEqual(result, {"items": ["a", "b", "c"]})

    def test_int_list(self):
        result = parse("nums = [1, 2, 3]")
        self.assertEqual(result, {"nums": [1, 2, 3]})

    def test_empty_list(self):
        self.assertEqual(parse("empty = []"), {"empty": []})


class TestComments(unittest.TestCase):
    def test_full_line_comment_skipped(self):
        text = """
        # this is a comment
        key = "value"
        """
        self.assertEqual(parse(text), {"key": "value"})

    def test_trailing_comment_stripped(self):
        self.assertEqual(parse('key = "v" # trailing'), {"key": "v"})

    def test_hash_inside_string_protected(self):
        self.assertEqual(parse('key = "a#b"'), {"key": "a#b"})

    def test_hash_inside_literal_string_protected(self):
        self.assertEqual(parse("key = 'a#b'"), {"key": "a#b"})


class TestTables(unittest.TestCase):
    """[section] / [a.b.c] の table ヘッダ。"""

    def test_simple_table(self):
        text = """
        [section]
        key = "value"
        """
        self.assertEqual(parse(text), {"section": {"key": "value"}})

    def test_nested_table(self):
        text = """
        [a.b.c]
        key = 1
        """
        self.assertEqual(parse(text), {"a": {"b": {"c": {"key": 1}}}})

    def test_multiple_tables(self):
        text = """
        [a]
        x = 1
        [b]
        y = 2
        """
        self.assertEqual(parse(text), {"a": {"x": 1}, "b": {"y": 2}})

    def test_dotted_key_inside_table(self):
        text = """
        [root]
        a.b = "deep"
        """
        # [root] の中で a.b = ... → root.a.b = ...
        self.assertEqual(parse(text), {"root": {"a": {"b": "deep"}}})


class TestErrors(unittest.TestCase):
    """構文エラーは TomlParseError で行番号入りで raise。"""

    def test_missing_equals(self):
        with self.assertRaises(TomlParseError) as ctx:
            parse("just_a_key")
        self.assertIn("'=' が見つかりません", str(ctx.exception))

    def test_unterminated_string(self):
        with self.assertRaises(TomlParseError):
            parse('key = "unclosed')

    def test_duplicate_key(self):
        with self.assertRaises(TomlParseError) as ctx:
            parse('key = "a"\nkey = "b"')
        self.assertIn("重複", str(ctx.exception))

    def test_empty_table_header(self):
        with self.assertRaises(TomlParseError):
            parse("[]")

    def test_unsupported_bool(self):
        # bool は非対応
        with self.assertRaises(TomlParseError):
            parse("flag = true")

    def test_unsupported_table_array(self):
        with self.assertRaises(TomlParseError) as ctx:
            parse("[[items]]")
        self.assertIn("table array", str(ctx.exception))

    def test_unsupported_escape(self):
        # \\u Unicode エスケープ (TOML 仕様にあるが本 subset 実装は非対応)
        with self.assertRaises(TomlParseError):
            parse('key = "\\u00ff"')

    def test_bare_key_with_invalid_char(self):
        # 日本語の bare key は禁止 → quote 必須
        with self.assertRaises(TomlParseError):
            parse("歩数 = 1")


class TestTableRedefinition(unittest.TestCase):
    """TOML 1.0 spec の table 再定義禁止 (codex 指摘 2026-04-29).

    https://toml.io/en/v1.0.0/#table の "you cannot define a table more than once".
    """

    def test_same_table_declared_twice_raises(self):
        text = "[a]\nx = 1\n[a]\ny = 2\n"
        with self.assertRaises(TomlParseError) as ctx:
            parse(text)
        self.assertIn("再宣言", str(ctx.exception))

    def test_dotted_key_then_table_raises(self):
        # a.b = 1 で implicit に作られた a を [a] で再宣言できない
        text = "a.b = 1\n[a]\nc = 2\n"
        with self.assertRaises(TomlParseError) as ctx:
            parse(text)
        self.assertIn("'a'", str(ctx.exception))

    def test_nested_table_then_parent_table_raises(self):
        # [a.b] で a が implicit に作られた後 [a] は禁止
        text = "[a.b]\nx = 1\n[a]\ny = 2\n"
        with self.assertRaises(TomlParseError):
            parse(text)


class TestLeadingZeroAndBom(unittest.TestCase):
    """leading zero 拒否 + BOM 対応 (codex nice-to-have 2026-04-29)."""

    def test_leading_zero_int_rejected(self):
        with self.assertRaises(TomlParseError):
            parse("n = 01")

    def test_zero_alone_accepted(self):
        self.assertEqual(parse("n = 0"), {"n": 0})

    def test_negative_zero_accepted(self):
        self.assertEqual(parse("n = -0"), {"n": 0})

    def test_bom_at_start_stripped(self):
        text = "﻿[a]\nx = 1\n"
        self.assertEqual(parse(text), {"a": {"x": 1}})

    def test_trailing_dot_in_key_rejected(self):
        # codex round 2 指摘: "a." のような末尾 dot は spec 違反だが silent 受理していた
        with self.assertRaises(TomlParseError):
            parse("a. = 1")

    def test_trailing_dot_in_table_header_rejected(self):
        with self.assertRaises(TomlParseError):
            parse("[a.]\nx = 1")

    def test_leading_dot_in_key_rejected(self):
        with self.assertRaises(TomlParseError):
            parse(".a = 1")


class TestQuotedStringStrict(unittest.TestCase):
    """quoted string の閉じ判定厳密化 (codex 指摘 2026-04-29 round 3).

    閉じる quote の後に余分な文字があるケース (typo の典型) を fail-fast.
    multiline string (\"\"\"...\"\"\" / '''...''') は本実装の subset 外として明示拒否.
    """

    def test_basic_string_extra_chars_after_close_rejected(self):
        # `"a"b"` は basic string の閉じ後に b" が余分
        with self.assertRaises(TomlParseError) as ctx:
            parse('key = "a"b"')
        self.assertIn("余分な文字", str(ctx.exception))

    def test_literal_string_extra_chars_after_close_rejected(self):
        with self.assertRaises(TomlParseError) as ctx:
            parse("key = 'a'b'")
        self.assertIn("余分な文字", str(ctx.exception))

    def test_array_with_malformed_basic_string_rejected(self):
        with self.assertRaises(TomlParseError):
            parse('key = ["a"b"]')

    def test_multiline_basic_string_rejected(self):
        # \"\"\"...\"\"\" は非対応として明示エラー
        with self.assertRaises(TomlParseError) as ctx:
            parse('key = """a"""')
        self.assertIn("multiline", str(ctx.exception))

    def test_multiline_literal_string_rejected(self):
        with self.assertRaises(TomlParseError) as ctx:
            parse("key = '''a'''")
        self.assertIn("multiline", str(ctx.exception))

    def test_unterminated_basic_string_rejected(self):
        with self.assertRaises(TomlParseError):
            parse('key = "no_close')

    def test_unterminated_literal_string_rejected(self):
        with self.assertRaises(TomlParseError):
            parse("key = 'no_close")


class TestQuotedKeyStrict(unittest.TestCase):
    """quoted key / table header の閉じ判定厳密化 (codex round 4 指摘 2026-04-29).

    値側 (basic / literal string) と同じ穴が _unquote_key にも残っていた.
    """

    def test_quoted_key_with_extra_chars_rejected(self):
        # 新 _split_dotted_key 実装では "余分な文字" ではなく
        # "予期しない文字" メッセージになる (segment 後 '.' 期待で失敗)
        with self.assertRaises(TomlParseError):
            parse('"a"b" = 1')

    def test_quoted_table_header_with_extra_chars_rejected(self):
        with self.assertRaises(TomlParseError):
            parse('["a"b"]\nx = 1')

    def test_quoted_literal_key_with_extra_chars_rejected(self):
        with self.assertRaises(TomlParseError):
            parse("'a'b' = 1")

    def test_valid_quoted_key_with_dot_inside(self):
        # quote 内のドットはちゃんと保護される (旧来の正常系を回帰)
        result = parse('"a.b" = 1')
        self.assertEqual(result, {"a.b": 1})

    def test_valid_japanese_quoted_key(self):
        result = parse('[root]\n"歩数" = "x"')
        self.assertEqual(result, {"root": {"歩数": "x"}})

    def test_quoted_key_with_escaped_quote_inside(self):
        # codex round 5 指摘: \\" を含む quoted key が誤って segment 切断されていた
        result = parse('"a\\"b" = 1')
        self.assertEqual(result, {'a"b': 1})

    def test_quoted_table_header_with_escaped_quote(self):
        result = parse('["a\\"b"]\nx = 1')
        self.assertEqual(result, {'a"b': {"x": 1}})

    def test_dotted_key_with_quoted_segment_then_bare(self):
        # "a.b".c と書いたとき "a.b" が 1 セグメント、 c が次のセグメント
        result = parse('"a.b".c = 1')
        self.assertEqual(result, {"a.b": {"c": 1}})

    def test_quoted_segment_with_escaped_quote_then_dotted(self):
        # "a\"b".c — quoted segment 内の \" を escape として認識し、その後 .c で続く
        result = parse('"a\\"b".c = 1')
        self.assertEqual(result, {'a"b': {"c": 1}})


class TestRealWorldConfig(unittest.TestCase):
    """実際の config.toml に近い形。"""

    def test_publishers_notion_fields_pattern(self):
        text = """
        # config.toml サンプル
        [publishers.notion.fields.step_count]
        property = "歩数 (歩)"

        [publishers.notion.fields.distance_km]
        property = "移動距離 (km)"

        [publishers.notion.fields.sleep_hours]
        property = "睡眠時間 (分)"
        """
        result = parse(text)
        self.assertEqual(
            result["publishers"]["notion"]["fields"]["step_count"]["property"],
            "歩数 (歩)",
        )
        self.assertEqual(
            result["publishers"]["notion"]["fields"]["distance_km"]["property"],
            "移動距離 (km)",
        )
        self.assertEqual(
            result["publishers"]["notion"]["fields"]["sleep_hours"]["property"],
            "睡眠時間 (分)",
        )

    def test_inline_comment_in_real_value(self):
        text = '[a]\nproperty = "歩数 (歩)" # 日本語 DB 名'
        self.assertEqual(parse(text), {"a": {"property": "歩数 (歩)"}})


if __name__ == "__main__":
    unittest.main()
