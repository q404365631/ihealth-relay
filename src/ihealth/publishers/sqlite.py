"""SQLite Publisher: 1 日 1 行を SQLite DB に UPSERT する Publisher (Phase 1 A3-sqlite).

オフライン分析向けのローカル DB. 例::

    python3 -m ihealth --publisher sqlite --sqlite-path ~/health.db
    sqlite3 ~/health.db 'SELECT * FROM daily_health WHERE step_count > 10000'

設計方針:
- **テーブルスキーマは ``daily_health`` 1 つ**. 列は ``date`` (PRIMARY KEY) +
  :data:`ihealth.publishers._payload.FIELD_SPECS` の 13 フィールド.
  ``int`` フィールドは ``INTEGER``, ``float`` は ``REAL``.
- **CREATE TABLE IF NOT EXISTS で初回起動可**. 既存 DB は無修正で動く.
- **UPSERT (``INSERT ... ON CONFLICT(date) DO UPDATE``)** で同じ日付の再実行が
  冪等. ``None`` フィールドは UPDATE 時に **既存値を保持**: ``COALESCE(excluded.x, x)``
  で「書き込もうとした値が NULL なら現在値を使う」セマンティクス. これは
  Notion publisher の「測ってない日は触らない」原則と同じ (= 今朝再実行しても
  夕方の手入力は消えない).
- **SQLite トランザクション**: ``BEGIN IMMEDIATE`` で明示開始 → ``CREATE``
  + ``UPSERT`` を 1 トランザクション → ``commit`` / ``rollback``. 失敗時に
  「DB ファイルだけ作られて中身空」のような中途半端な状態を残さない.
- **明示 close**: ``try/finally`` で ``conn.close()`` を保証. ``with sqlite3.connect``
  は context manager 抜けるとき commit/rollback はするが close はしないため.
- **schema drift 検知**: 既存 DB に :data:`FIELD_SPECS` の列が不足しているとき
  fail-fast (= migration 必要を明示). silent に SQL エラーで死ぬよりユーザーが
  原因を特定しやすい.
- **外部依存ゼロ**: stdlib ``sqlite3`` だけ.
- **threading 安全性**: 1 publish ごとに connect/commit/close する短命接続.
  SQLite の WAL モードや busy_timeout は ``timeout`` で短く (single writer の
  daily batch 想定でロック競合は想定しない). 万一競合したら fail-fast.

I/O / DB 失敗は :class:`SQLitePublishError` に統一して raise.
workflow 層が :class:`SQLiteAppError` (``exit_code=9``) にラップする.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from pathlib import Path

from ihealth.models import DailyHealthData
from ihealth.publishers._payload import (
    FIELD_SPECS,
    PayloadValidationError,
    build_payload,
)


#: テーブル名. 将来 metric 別テーブルに分けることも考えたが、 1 日 1 行の
#: 集計値だけなら 1 テーブルで十分シンプル. 名前空間が衝突する別アプリは
#: 自分で別 DB ファイル (= 別 ``--sqlite-path``) を用意する想定.
TABLE_NAME = "daily_health"


def _column_definition() -> str:
    """``CREATE TABLE`` 時の列定義 SQL を組み立てる.

    ``date`` は ``TEXT PRIMARY KEY`` (ISO 8601 ``YYYY-MM-DD`` 文字列).
    他は :data:`FIELD_SPECS` から ``int`` → ``INTEGER``, ``float`` → ``REAL``.
    全列 NULLable (= 集計欠落を表現する Notion / Markdown と同じセマンティクス).
    """
    cols = ["date TEXT PRIMARY KEY"]
    for name, spec in FIELD_SPECS.items():
        sql_type = "INTEGER" if spec.kind == "int" else "REAL"
        cols.append(f"{name} {sql_type}")
    return ", ".join(cols)


def _create_table_sql() -> str:
    return f"CREATE TABLE IF NOT EXISTS {TABLE_NAME} ({_column_definition()})"


def _upsert_sql() -> str:
    """``INSERT ... ON CONFLICT(date) DO UPDATE`` を組み立てる.

    ``UPDATE`` 部分は ``COALESCE(excluded.x, x)`` で **NULL は既存値保持**.
    これにより:
    - 再実行で同じ日に書く: 新しい値で上書き
    - 一部フィールドが ``None`` (測ってない / 集計失敗) で再実行: 既存値を保持

    SQLite 3.24+ の ``ON CONFLICT`` 構文を使う. macOS 14+ の bundled SQLite
    は 3.39+ なので問題なし. 一応 :func:`_assert_sqlite_version` で検証する.
    """
    placeholders = ":date, " + ", ".join(f":{name}" for name in FIELD_SPECS)
    update_pairs = ", ".join(
        f"{name} = COALESCE(excluded.{name}, {TABLE_NAME}.{name})"
        for name in FIELD_SPECS
    )
    return (
        f"INSERT INTO {TABLE_NAME} (date, {', '.join(FIELD_SPECS)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(date) DO UPDATE SET {update_pairs}"
    )


def _assert_no_schema_drift(conn: "sqlite3.Connection") -> None:
    """既存 DB のスキーマが :data:`FIELD_SPECS` と一致するか検証する.

    publisher のフィールドが追加
    された後に、古いスキーマの DB に書き込もうとすると ``no column named ...``
    で SQL エラーになる. これは silent ではないが、ユーザーが原因を特定し
    やすいよう ``SQLitePublishError`` に意味のあるメッセージで rewrap する.

    存在しない (= ``CREATE TABLE IF NOT EXISTS`` で作りたて) ケースでは
    PRAGMA が空を返すので drift 判定は通過する. ``CREATE TABLE`` の直後に
    呼ぶこと (= テーブル無しで判定するとスキーマ不整合と誤判定する).
    """
    cursor = conn.execute(f"PRAGMA table_info({TABLE_NAME})")
    cols = {row[1] for row in cursor.fetchall()}
    if not cols:
        # ``IF NOT EXISTS`` で作ったが PRAGMA 結果が空 = 異常事態. 通常は到達しない.
        return  # pragma: no cover
    expected = {"date", *FIELD_SPECS.keys()}
    missing = expected - cols
    if missing:
        raise SQLitePublishError(
            "SQLite スキーマ不一致: 既存 DB に列が不足しています "
            f"(missing={sorted(missing)}). "
            "DB を再作成するか migration スクリプトで追加してください."
        )


_MIN_SQLITE_VERSION = (3, 24, 0)


def _assert_sqlite_version() -> None:
    """``ON CONFLICT`` 構文を使うため SQLite >= 3.24 を要求する.

    Python に bundled された ``sqlite3`` の version を見る (= ``sqlite3.sqlite_version``).
    macOS 14+ の system Python では 3.39+ なので普通は通る.
    """
    parts = tuple(int(p) for p in sqlite3.sqlite_version.split(".")[:3])
    if parts < _MIN_SQLITE_VERSION:
        raise SQLitePublishError(
            f"SQLite {'.'.join(str(p) for p in _MIN_SQLITE_VERSION)} 以上が必要です: "
            f"現在 {sqlite3.sqlite_version}"
        )


class SQLitePublishError(RuntimeError):
    """SQLite publisher の I/O / DB / 値検証で発生する例外."""


class SQLitePublisher:
    """``DailyHealthData`` を SQLite DB の ``daily_health`` テーブルに UPSERT.

    Args:
        db_path: SQLite DB ファイルパス. 親ディレクトリは自動で作る.
        target_date: 対象日. ``health_data.date`` との不一致を弾く.
        logger: 注入ロガー.

    Raises (publish 内):
        SQLitePublishError: target_date 不整合 / 値検証失敗 / DB 書き込み失敗.
    """

    def __init__(
        self,
        db_path: Path,
        target_date: date,
        logger: logging.Logger,
    ) -> None:
        self._db_path = db_path
        self._target_date = target_date
        self._logger = logger

    def publish(self, health_data: DailyHealthData) -> None:
        if health_data.date != self._target_date:
            raise SQLitePublishError(
                f"target_date 不整合: publisher={self._target_date} "
                f"data={health_data.date}"
            )

        try:
            payload = build_payload(health_data)
        except PayloadValidationError as exc:
            raise SQLitePublishError(str(exc)) from exc

        # ``payload`` は ``date`` + populated fields のみ. 全フィールドを
        # parameter として渡すために None 埋めする.
        params: "dict[str, object]" = {"date": payload["date"]}
        for name in FIELD_SPECS:
            params[name] = payload.get(name)  # 不在キーは None

        # 親ディレクトリを作る (db_path = ~/data/health.db のようなネスト対応).
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SQLitePublishError(
                f"DB ディレクトリ作成に失敗: {self._db_path.parent} ({exc})"
            ) from exc

        try:
            _assert_sqlite_version()
            # - ``with sqlite3.connect`` は commit/rollback はしても close は
            #   保証しない. 短命接続にしたいので try/finally で close を強制.
            # - ``BEGIN IMMEDIATE`` を **CREATE TABLE より先** に置き、
            #   CREATE TABLE → schema drift check → UPSERT を 1 atomic 単位にして
            #   「UPSERT 失敗時に空テーブルだけ残る」中途半端な状態を起こさない.
            # - ``timeout`` は launchd one-shot 前提なので短く (10 秒).
            #   競合するなら fail-fast の方が運用上わかりやすい.
            # - ``conn.in_transaction`` で rollback の安全性を担保 (BEGIN 前に
            #   失敗したら in_transaction=False で rollback はスキップ).
            conn = sqlite3.connect(str(self._db_path), timeout=10.0)
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(_create_table_sql())
                    _assert_no_schema_drift(conn)
                    conn.execute(_upsert_sql(), params)
                except Exception:
                    if conn.in_transaction:
                        conn.rollback()
                    raise
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise SQLitePublishError(
                f"SQLite 書き込みに失敗: {self._db_path} ({exc})"
            ) from exc

        # populated 件数 = payload のキー数 - 1 (date を除く)
        populated = len(payload) - 1
        self._logger.info(
            "SQLite UPSERT 成功: %s (populated=%d)", self._db_path, populated,
        )
