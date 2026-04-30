"""最小限の TOML パーサ (Phase 1 B1 / Issue #18).

Python 3.9 互換のため :mod:`tomllib` (3.11+) は使えず、外部依存ゼロ原則のため
``tomli`` 等の vendoring も避ける。本プロジェクトの ``config.toml`` が使う
subset だけを実装した手書き最小パーサ。

サポートする TOML 構文 (本プロジェクトの設定ファイルに必要なもの):
    - ``key = "string"`` / ``key = 'string'``  (basic / literal string)
    - ``key = 123``                              (整数)
    - ``key = ["a", "b"]``                       (string 配列)
    - ``[section]`` / ``[section.subsection.key]`` (table / 入れ子 table)
    - ``# comment`` 行 / 行末コメント
    - 空行スキップ

非サポート (現時点で必要ない):
    - multiline string (``\"\"\"...\"\"\"``)
    - datetime / float / boolean
    - inline table (``key = { a = 1 }``)
    - table array (``[[name]]``)
    - 文字列内の高度なエスケープ (``\\u`` ``\\x`` ``\\b`` 等。``\\n`` ``\\t``
      ``\\\"`` ``\\\\`` のみ basic string で対応)

将来的にこれらが必要になったら、``tomli`` vendoring または Python 3.11+
への移行を検討する。

例::

    >>> data = parse(\"\"\"\n... [publishers.notion]\n... database_id = \"abc\"\n... [publishers.notion.fields.step_count]\n... property = \"歩数 (歩)\"\n... \"\"\")
    >>> data["publishers"]["notion"]["database_id"]
    'abc'
    >>> data["publishers"]["notion"]["fields"]["step_count"]["property"]
    '歩数 (歩)'
"""

from __future__ import annotations

import re


#: integer 値の正規表現. TOML 1.0 spec: ``[+-]?(0|[1-9][0-9]*)``.
#: leading zero (``01`` 等) は不正. underscore 区切りも未対応.
_INT_PATTERN = re.compile(r"^[+-]?(0|[1-9][0-9]*)$")


class TomlParseError(ValueError):
    """TOML パース時のエラー。行番号と原因を含む。"""


def parse(text: str) -> "dict[str, object]":
    """TOML 文字列を Python dict に変換する。

    Args:
        text: TOML 形式の文字列 (改行区切り)。先頭 BOM (U+FEFF) は除去する。

    Raises:
        TomlParseError: 構文エラー (重複キー、table 再定義、括弧不一致、未知の値型 など)

    Returns:
        ネストした dict。table は ``dict``、配列は ``list`` として表現される。

    TOML 1.0 spec の table redefinition rule (https://toml.io/en/v1.0.0/#table):
        - 同じ table を 2 回 ``[name]`` で宣言するのは禁止
        - dotted key (``a.b = 1``) で implicit に作られた中間 table を、後から
          ``[a]`` で再宣言するのも禁止
        本実装では declared_tables / implicit_tables を track して両方検出する。
    """
    # BOM (U+FEFF) は単純に剥がす — Windows 系エディタでの保存事故対策
    if text.startswith("﻿"):
        text = text[1:]

    root: "dict[str, object]" = {}
    current_table: "dict[str, object]" = root
    current_table_path: "tuple[str, ...]" = ()
    # 明示的に [...] で宣言された table のキーパス
    declared_tables: "set[tuple[str, ...]]" = set()
    # dotted key 経由 / 中間 table として暗黙に作られたキーパス
    implicit_tables: "set[tuple[str, ...]]" = set()

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw_line).strip()
        if not line:
            continue

        if line.startswith("[") and line.endswith("]"):
            # table header
            if line.startswith("[["):
                raise TomlParseError(
                    f"line {lineno}: table array ([[...]]) は未サポートです"
                )
            path_str = line[1:-1].strip()
            if not path_str:
                raise TomlParseError(f"line {lineno}: 空の table 名です: {raw_line!r}")
            new_path_list = _split_dotted_key(path_str, lineno)
            new_path = tuple(new_path_list)
            if new_path in declared_tables:
                raise TomlParseError(
                    f"line {lineno}: table {'.'.join(new_path_list)!r} が既に宣言されています "
                    "(TOML 1.0 spec: 同じ table の再宣言は禁止)"
                )
            if new_path in implicit_tables:
                raise TomlParseError(
                    f"line {lineno}: table {'.'.join(new_path_list)!r} は dotted key で "
                    "暗黙に作成済みのため [...] で再宣言できません "
                    "(TOML 1.0 spec)"
                )
            # 親パス (a.b.c の a / a.b) は implicit として登録 (後続の [a] / [a.b] を防ぐ)
            for i in range(1, len(new_path)):
                implicit_tables.add(new_path[:i])
            current_table = _ensure_table(root, new_path_list, lineno)
            current_table_path = new_path
            declared_tables.add(new_path)
            continue

        if "=" not in line:
            raise TomlParseError(
                f"line {lineno}: '=' が見つかりません: {raw_line!r}"
            )
        key_part, _, value_part = line.partition("=")
        key_path = _split_dotted_key(key_part.strip(), lineno)
        value = _parse_value(value_part.strip(), lineno)

        # dotted key の場合は中間 table を作って末端に値を入れる。
        # 中間 table は implicit_tables に登録 (TOML spec の再定義検出のため).
        target = current_table
        for i, segment in enumerate(key_path[:-1]):
            target = _descend_or_create(target, segment, lineno)
            implicit_tables.add(current_table_path + tuple(key_path[: i + 1]))
        leaf = key_path[-1]
        if leaf in target:
            raise TomlParseError(
                f"line {lineno}: キーが重複しています: {'.'.join(key_path)!r}"
            )
        target[leaf] = value

    return root


def _strip_comment(line: str) -> str:
    """行末コメント (``#`` 以降) を除去する。

    ただし string リテラル (``"..."`` / ``'...'``) 内の ``#`` は保護する。
    """
    in_basic = False  # "..."
    in_literal = False  # '...'
    escape_next = False
    for i, ch in enumerate(line):
        if escape_next:
            escape_next = False
            continue
        if in_basic:
            if ch == "\\":
                escape_next = True
            elif ch == '"':
                in_basic = False
        elif in_literal:
            if ch == "'":
                in_literal = False
        else:
            if ch == "#":
                return line[:i]
            if ch == '"':
                in_basic = True
            elif ch == "'":
                in_literal = True
    return line


def _split_dotted_key(raw: str, lineno: int) -> "list[str]":
    """``a.b.c`` を ``["a", "b", "c"]`` に分割する.

    quoted segment (``"..."`` / ``'...'``) は scan ベースで正しく消費し、
    内部の escape (``\\"``, ``\\\\``) や ドット (``"a.b"`` で 1 セグメント)、
    閉じ quote 後の余分な文字を全て検出する.

    - 末尾 ``.`` (例: ``a.``) と先頭 ``.`` (例: ``.a``) は TOML 1.0 spec 違反として弾く
    - bare key は ASCII 英数 + ``_`` ``-`` のみ (CJK は quote 必須)
    """
    s = raw.strip()
    if not s:
        raise TomlParseError(f"line {lineno}: キーが空です: {raw!r}")
    if s.startswith("."):
        raise TomlParseError(f"line {lineno}: 先頭の '.' が不正です: {raw!r}")
    if s.endswith("."):
        raise TomlParseError(f"line {lineno}: 末尾の '.' が不正です: {raw!r}")

    segments: "list[str]" = []
    i = 0
    expect_segment = True
    while i < len(s):
        # 区切り '.' 周辺の空白を許す (TOML 仕様: a.b と a . b は等価)
        while i < len(s) and s[i] in " \t":
            i += 1
        if i >= len(s):
            break
        ch = s[i]

        if not expect_segment:
            # segment の直後は '.' 期待
            if ch != ".":
                raise TomlParseError(
                    f"line {lineno}: dotted key で予期しない文字 {ch!r}: {raw!r}"
                )
            i += 1
            expect_segment = True
            continue

        # ここから新しい segment を消費する
        if ch == '"':
            body, rest = _scan_basic_string(s[i:], lineno)
            segments.append(body)
            i = len(s) - len(rest)
        elif ch == "'":
            body, rest = _scan_literal_string(s[i:], lineno)
            segments.append(body)
            i = len(s) - len(rest)
        else:
            # bare segment: 次の '.' か空白か末尾まで
            j = i
            while j < len(s) and s[j] not in ". \t":
                j += 1
            seg = s[i:j]
            if not seg:
                raise TomlParseError(
                    f"line {lineno}: 空のキーセグメントです: {raw!r}"
                )
            for c in seg:
                if not (c.isascii() and (c.isalnum() or c in "_-")):
                    raise TomlParseError(
                        f"line {lineno}: bare key に不正な文字: {seg!r} "
                        "(quote するか ASCII 英数 _ - のみ)"
                    )
            segments.append(seg)
            i = j
        expect_segment = False

    if not segments:
        raise TomlParseError(f"line {lineno}: キーが空です")
    if expect_segment:
        # ループ内の '.' を消費した直後に segment が来なかった = 末尾 dot
        # (上の endswith('.') チェックで既に弾けるが二重保険)
        raise TomlParseError(f"line {lineno}: 末尾の '.' が不正です: {raw!r}")
    return segments


def _ensure_table(
    root: "dict[str, object]", path: "list[str]", lineno: int,
) -> "dict[str, object]":
    """``[a.b.c]`` ヘッダに対して、root から辿って中間 table を作る。"""
    target: "dict[str, object]" = root
    for segment in path:
        target = _descend_or_create(target, segment, lineno)
    return target


def _descend_or_create(
    table: "dict[str, object]", key: str, lineno: int,
) -> "dict[str, object]":
    if key not in table:
        new_table: "dict[str, object]" = {}
        table[key] = new_table
        return new_table
    existing = table[key]
    if not isinstance(existing, dict):
        raise TomlParseError(
            f"line {lineno}: {key!r} は既に値として定義済みなので table にできません"
        )
    return existing


def _parse_value(raw: str, lineno: int) -> object:
    """``=`` の右辺を Python 値に変換する。"""
    s = raw.strip()
    if not s:
        raise TomlParseError(f"line {lineno}: 値が空です")
    if s[0] == '"':
        return _parse_basic_string(s, lineno)
    if s[0] == "'":
        return _parse_literal_string(s, lineno)
    if s[0] == "[":
        return _parse_array(s, lineno)
    if s in ("true", "false"):
        raise TomlParseError(f"line {lineno}: bool は未サポート (本プロジェクトで未使用)")
    # integer (符号 + 10 進数のみ)
    # TOML 1.0 spec: leading zero は禁止 (``01`` のような表記は不正)
    # 例外的に ``0`` 単体は OK。``-0`` ``+0`` も OK。
    if _INT_PATTERN.fullmatch(s):
        return int(s)
    raise TomlParseError(
        f"line {lineno}: 値の型を判別できません: {raw!r} "
        "(サポート: \"string\" / 'string' / int (leading-zero 不可) / [...])"
    )


def _parse_basic_string(s: str, lineno: int) -> str:
    """``"..."`` を unescape して返す (multiline 不可)。

    閉じる ``"`` の後ろに余分な文字があれば TomlParseError. これにより
    ``key = "a"b"`` のような spec 違反を silent 受理しない.
    """
    body, rest = _scan_basic_string(s, lineno)
    if rest:
        raise TomlParseError(
            f"line {lineno}: basic string の閉じ '\"' の後に余分な文字: {rest!r}"
        )
    return body


def _parse_literal_string(s: str, lineno: int) -> str:
    """``'...'`` をそのまま返す (escape 解釈なし、multiline 不可)。

    閉じる ``'`` の後ろに余分な文字があれば TomlParseError.
    """
    body, rest = _scan_literal_string(s, lineno)
    if rest:
        raise TomlParseError(
            f"line {lineno}: literal string の閉じ \"'\" の後に余分な文字: {rest!r}"
        )
    return body


def _scan_basic_string(s: str, lineno: int) -> "tuple[str, str]":
    """basic string ``"..."`` を 1 つだけ消費し ``(body, rest)`` を返す.

    入力は ``"`` で始まる前提. multiline (``\"\"\"...\"\"\"``) は非対応のため、
    ``""`` の直後に ``"`` が続く形 (= multiline 開始) は明示的にエラーにする.
    """
    if not s.startswith('"'):
        raise TomlParseError(f"line {lineno}: basic string は '\"' で始まる必要があります: {s!r}")
    # multiline (\"\"\") は非対応 — 開始 3 連続 quote を弾く
    if s.startswith('"""'):
        raise TomlParseError(
            f"line {lineno}: multiline basic string (\"\"\"...\"\"\") は未サポートです"
        )
    chars: "list[str]" = []
    i = 1
    while i < len(s):
        ch = s[i]
        if ch == "\\":
            if i + 1 >= len(s):
                raise TomlParseError(
                    f"line {lineno}: basic string の末尾の '\\' が不正です"
                )
            nxt = s[i + 1]
            mapping = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "r": "\r"}
            if nxt not in mapping:
                raise TomlParseError(
                    f"line {lineno}: 未対応のエスケープ: '\\{nxt}' "
                    "(本実装では \\n \\t \\\" \\\\ \\r のみ対応)"
                )
            chars.append(mapping[nxt])
            i += 2
            continue
        if ch == '"':
            return "".join(chars), s[i + 1:]
        chars.append(ch)
        i += 1
    raise TomlParseError(f"line {lineno}: basic string が閉じられていません: {s!r}")


def _scan_literal_string(s: str, lineno: int) -> "tuple[str, str]":
    """literal string ``'...'`` を 1 つだけ消費し ``(body, rest)`` を返す."""
    if not s.startswith("'"):
        raise TomlParseError(f"line {lineno}: literal string は \"'\" で始まる必要があります: {s!r}")
    if s.startswith("'''"):
        raise TomlParseError(
            f"line {lineno}: multiline literal string ('''...''') は未サポートです"
        )
    end_pos = s.find("'", 1)
    if end_pos < 0:
        raise TomlParseError(f"line {lineno}: literal string が閉じられていません: {s!r}")
    return s[1:end_pos], s[end_pos + 1:]


def _unescape_basic(s: str) -> str:
    """basic string の最小 escape 集合を解釈する (\\n, \\t, \\\", \\\\)."""
    result: "list[str]" = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch != "\\":
            result.append(ch)
            i += 1
            continue
        if i + 1 >= len(s):
            raise TomlParseError(f"basic string の末尾の '\\' が不正です: {s!r}")
        nxt = s[i + 1]
        mapping = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "r": "\r"}
        if nxt not in mapping:
            raise TomlParseError(
                f"未対応のエスケープ: '\\{nxt}' (本プロジェクトでは \\n \\t \\\" \\\\ \\r のみ対応)"
            )
        result.append(mapping[nxt])
        i += 2
    return "".join(result)


def _parse_array(s: str, lineno: int) -> "list[object]":
    """``[a, b, c]`` を list に変換する (1 行配列のみ)。"""
    if not (s.startswith("[") and s.endswith("]")):
        raise TomlParseError(f"line {lineno}: 配列が ']' で閉じていません: {s!r}")
    inner = s[1:-1].strip()
    if not inner:
        return []
    items: "list[object]" = []
    # 単純な split (string 内の , は protect)
    buf: "list[str]" = []
    in_basic = False
    in_literal = False
    escape_next = False
    for ch in inner:
        if escape_next:
            buf.append(ch)
            escape_next = False
            continue
        if in_basic:
            buf.append(ch)
            if ch == "\\":
                escape_next = True
            elif ch == '"':
                in_basic = False
            continue
        if in_literal:
            buf.append(ch)
            if ch == "'":
                in_literal = False
            continue
        if ch == ",":
            items.append(_parse_value("".join(buf).strip(), lineno))
            buf = []
            continue
        if ch == '"':
            in_basic = True
        elif ch == "'":
            in_literal = True
        buf.append(ch)
    if buf:
        items.append(_parse_value("".join(buf).strip(), lineno))
    return items
