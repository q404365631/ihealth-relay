"""SQLite Publisher (Phase 1 A3-sqlite) の回帰テスト.

DB ファイルは ``tempfile.TemporaryDirectory`` 配下に作るので実環境を汚さない.
Network も Notion も触らない.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from ihealth.models import DailyHealthData
from ihealth.publishers._payload import FIELD_SPECS
from ihealth.publishers.sqlite import (
    SQLitePublishError,
    SQLitePublisher,
    TABLE_NAME,
    _create_table_sql,
    _upsert_sql,
)


def _logger() -> logging.Logger:
    return logging.getLogger("test-sqlite-publisher")


class TestCreateTableSQL(unittest.TestCase):
    def test_table_name(self):
        sql = _create_table_sql()
        self.assertIn(TABLE_NAME, sql)

    def test_date_is_primary_key(self):
        sql = _create_table_sql()
        self.assertIn("date TEXT PRIMARY KEY", sql)

    def test_step_count_is_integer(self):
        # int kind → INTEGER 列
        sql = _create_table_sql()
        self.assertIn("step_count INTEGER", sql)
        self.assertIn("mindful_sessions INTEGER", sql)

    def test_distance_km_is_real(self):
        # float kind → REAL 列
        sql = _create_table_sql()
        self.assertIn("distance_km REAL", sql)
        self.assertIn("body_mass_kg REAL", sql)

    def test_idempotent_creation(self):
        # IF NOT EXISTS が含まれる → 2 回目以降の起動でエラーにならない
        self.assertIn("IF NOT EXISTS", _create_table_sql())


class TestUpsertSQL(unittest.TestCase):
    def test_includes_on_conflict_date(self):
        sql = _upsert_sql()
        self.assertIn("ON CONFLICT(date)", sql)

    def test_uses_coalesce_for_null_preservation(self):
        # NULL 書き込み時に既存値を保持するため COALESCE が必須
        sql = _upsert_sql()
        self.assertIn("COALESCE", sql)

    def test_named_parameters(self):
        sql = _upsert_sql()
        self.assertIn(":date", sql)
        self.assertIn(":step_count", sql)


class TestSQLitePublisherBasics(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "health.db"
        self.target_date = date(2026, 4, 22)
        self.log = MagicMock()
        self.pub = SQLitePublisher(
            db_path=self.db_path,
            target_date=self.target_date,
            logger=self.log,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_db_file(self):
        data = DailyHealthData(date=self.target_date, step_count=100)
        self.pub.publish(data)
        self.assertTrue(self.db_path.is_file())

    def test_creates_parent_directory(self):
        nested = Path(self._tmp.name) / "nested" / "deep" / "health.db"
        pub = SQLitePublisher(
            db_path=nested, target_date=self.target_date, logger=self.log,
        )
        data = DailyHealthData(date=self.target_date, step_count=100)
        pub.publish(data)
        self.assertTrue(nested.is_file())

    def test_inserts_row(self):
        data = DailyHealthData(
            date=self.target_date, step_count=12345, distance_km=5.234,
        )
        self.pub.publish(data)
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                f"SELECT date, step_count, distance_km FROM {TABLE_NAME} "
                f"WHERE date = ?",
                (self.target_date.isoformat(),),
            ).fetchone()
        self.assertEqual(row[0], "2026-04-22")
        self.assertEqual(row[1], 12345)
        self.assertEqual(row[2], 5.23)  # round 2

    def test_target_date_mismatch_raises(self):
        wrong = DailyHealthData(date=date(2026, 4, 23), step_count=1)
        with self.assertRaises(SQLitePublishError):
            self.pub.publish(wrong)

    def test_invalid_value_raises(self):
        # parser bug で step_count=True 等 → fail-fast
        data = DailyHealthData.__new__(DailyHealthData)
        object.__setattr__(data, "date", self.target_date)
        for f in FIELD_SPECS:
            object.__setattr__(data, f, None)
        object.__setattr__(data, "step_count", True)
        with self.assertRaises(SQLitePublishError) as ctx:
            self.pub.publish(data)
        self.assertIn("step_count", str(ctx.exception))


class TestSQLitePublisherUpsert(unittest.TestCase):
    """同じ日付の再実行で UPSERT する (= idempotent)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "health.db"
        self.target_date = date(2026, 4, 22)
        self.log = MagicMock()
        self.pub = SQLitePublisher(
            db_path=self.db_path,
            target_date=self.target_date,
            logger=self.log,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _select_row(self) -> "tuple":
        with sqlite3.connect(str(self.db_path)) as conn:
            return conn.execute(
                f"SELECT step_count, distance_km, sleep_hours FROM {TABLE_NAME} "
                f"WHERE date = ?",
                (self.target_date.isoformat(),),
            ).fetchone()

    def test_second_publish_overwrites_with_new_values(self):
        first = DailyHealthData(
            date=self.target_date, step_count=100, distance_km=1.0,
        )
        second = DailyHealthData(
            date=self.target_date, step_count=200, distance_km=2.0,
        )
        self.pub.publish(first)
        self.pub.publish(second)
        row = self._select_row()
        self.assertEqual(row[0], 200)
        self.assertEqual(row[1], 2.0)

    def test_none_field_preserves_existing(self):
        # 1 回目に sleep_hours=7.0 を書き、2 回目に sleep_hours=None で再実行
        # → sleep_hours の既存値 7.0 が保持される (Notion publisher と同じ
        # 「測ってない日は触らない」セマンティクス).
        first = DailyHealthData(
            date=self.target_date, step_count=100, sleep_hours=7.0,
        )
        second = DailyHealthData(
            date=self.target_date, step_count=200,  # sleep_hours=None
        )
        self.pub.publish(first)
        self.pub.publish(second)
        row = self._select_row()
        self.assertEqual(row[0], 200)   # step_count は更新
        self.assertEqual(row[2], 7.0)   # sleep_hours は保持

    def test_only_one_row_per_date(self):
        for i in range(5):
            self.pub.publish(DailyHealthData(date=self.target_date, step_count=i))
        with sqlite3.connect(str(self.db_path)) as conn:
            count = conn.execute(
                f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE date = ?",
                (self.target_date.isoformat(),),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_multiple_dates_coexist(self):
        d1 = date(2026, 4, 22)
        d2 = date(2026, 4, 23)
        SQLitePublisher(
            db_path=self.db_path, target_date=d1, logger=self.log,
        ).publish(DailyHealthData(date=d1, step_count=100))
        SQLitePublisher(
            db_path=self.db_path, target_date=d2, logger=self.log,
        ).publish(DailyHealthData(date=d2, step_count=200))
        with sqlite3.connect(str(self.db_path)) as conn:
            count = conn.execute(
                f"SELECT COUNT(*) FROM {TABLE_NAME}"
            ).fetchone()[0]
        self.assertEqual(count, 2)


class TestSQLitePublisherErrors(unittest.TestCase):
    def test_unwritable_path_raises(self):
        # parent 配下にファイルがある → ディレクトリ作成失敗
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "actually_a_file"
            blocker.write_text("x", encoding="utf-8")
            broken = blocker / "subdir" / "health.db"
            pub = SQLitePublisher(
                db_path=broken,
                target_date=date(2026, 4, 22),
                logger=MagicMock(),
            )
            data = DailyHealthData(date=date(2026, 4, 22), step_count=1)
            with self.assertRaises(SQLitePublishError) as ctx:
                pub.publish(data)
            self.assertIn("DB ディレクトリ作成", str(ctx.exception))


class TestSQLiteSchemaCompat(unittest.TestCase):
    """codex round 1 PR #23 指摘: schema drift 検知 / lock 競合."""

    def test_schema_drift_raises_publish_error(self):
        # 既存 DB に古いスキーマがあると新規列で SQL エラー → SQLitePublishError
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "old.db"
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(f"CREATE TABLE {TABLE_NAME} (date TEXT PRIMARY KEY)")
                # step_count 列など全部欠落
            pub = SQLitePublisher(
                db_path=db_path, target_date=date(2026, 4, 22),
                logger=MagicMock(),
            )
            with self.assertRaises(SQLitePublishError) as ctx:
                pub.publish(DailyHealthData(date=date(2026, 4, 22), step_count=1))
            self.assertIn("スキーマ不一致", str(ctx.exception))
            self.assertIn("step_count", str(ctx.exception))

    def test_lock_contention_raises_within_timeout(self):
        """別プロセスが排他ロック取ったまま → publisher は timeout 後 fail-fast.

        timeout=10 だとテストが遅いので、lock を即解放してから publish する.
        timeout=10 で lock が継続するケースは production シナリオで発生したら
        SQLitePublishError("database is locked") に rewrap されることを確認.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "x.db"
            # 1 回 publish して DB 作成
            pub = SQLitePublisher(
                db_path=db_path, target_date=date(2026, 4, 22),
                logger=MagicMock(),
            )
            pub.publish(DailyHealthData(date=date(2026, 4, 22), step_count=1))

            # 別 connection で BEGIN EXCLUSIVE → lock 維持中に publish 試行
            locker = sqlite3.connect(str(db_path), timeout=0.1)
            try:
                locker.execute("BEGIN EXCLUSIVE")
                # publisher は timeout=10 で待つので test を遅くしないため
                # publisher 側の timeout を patch で短縮する
                from unittest.mock import patch
                original_connect = sqlite3.connect

                def fast_timeout_connect(*args, **kwargs):
                    kwargs["timeout"] = 0.5
                    return original_connect(*args, **kwargs)

                with patch("ihealth.publishers.sqlite.sqlite3.connect",
                           side_effect=fast_timeout_connect):
                    with self.assertRaises(SQLitePublishError) as ctx:
                        pub.publish(DailyHealthData(date=date(2026, 4, 22), step_count=2))
                self.assertIn("locked", str(ctx.exception).lower())
            finally:
                locker.rollback()
                locker.close()


class _ConnSpy:
    """``sqlite3.Connection`` の薄いラッパで rollback / commit の呼び出しを記録.

    Python 3.9 の sqlite3.Connection は subclass 不可属性を持つため、
    composition ベースで必要メソッドのみ proxy する.
    """

    def __init__(self, real: "sqlite3.Connection") -> None:
        self._real = real
        self.rollback_called = False
        self.commit_called = False

    @property
    def in_transaction(self) -> bool:
        return self._real.in_transaction

    def execute(self, *args, **kwargs):
        return self._real.execute(*args, **kwargs)

    def commit(self) -> None:
        self.commit_called = True
        self._real.commit()

    def rollback(self) -> None:
        self.rollback_called = True
        self._real.rollback()

    def close(self) -> None:
        self._real.close()


class TestSQLiteAtomicity(unittest.TestCase):
    """codex round 2 / 3 PR #23 指摘: CREATE TABLE + UPSERT 全体の atomic 性を回帰検証."""

    def test_create_table_rolled_back_on_upsert_failure(self):
        """新規 DB で UPSERT が失敗したら ``daily_health`` table が残らない.

        ``_upsert_sql`` を必ず失敗するように差し替えて publish. publish() は
        SQLitePublishError で raise されるが、rollback で CREATE TABLE 自体
        も巻き戻されて DB に table が残らないことを確認.
        """
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "fresh.db"
            pub = SQLitePublisher(
                db_path=db_path, target_date=date(2026, 4, 22),
                logger=MagicMock(),
            )

            # _upsert_sql() を意図的に壊す → SQL execute で OperationalError
            with patch(
                "ihealth.publishers.sqlite._upsert_sql",
                return_value="INSERT INTO bogus_table_does_not_exist VALUES (?)",
            ):
                with self.assertRaises(SQLitePublishError):
                    pub.publish(DailyHealthData(date=date(2026, 4, 22), step_count=1))

            # DB ファイルは sqlite3.connect 時点で作られるので存在する.
            # しかし daily_health table は rollback で残らない (atomic).
            self.assertTrue(db_path.exists())
            with sqlite3.connect(str(db_path)) as conn:
                tables = [
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                ]
            self.assertNotIn(TABLE_NAME, tables)

    def test_explicit_rollback_called_on_failure(self):
        """codex round 3 nice-to-have: rollback() の **明示呼び出し** を固定.

        atomic test だけだと ``conn.close()`` 時の implicit rollback でも通る.
        ``_ConnSpy`` で rollback/commit の呼び出しフラグを観測して、失敗経路で
        rollback() がちゃんと呼ばれていること (= close 任せにしていないこと)
        を保証する.
        """
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "spy.db"
            spies: "list[_ConnSpy]" = []
            real_connect = sqlite3.connect

            def spy_connect(*args, **kwargs):
                spy = _ConnSpy(real_connect(*args, **kwargs))
                spies.append(spy)
                return spy

            pub = SQLitePublisher(
                db_path=db_path, target_date=date(2026, 4, 22),
                logger=MagicMock(),
            )
            with patch("ihealth.publishers.sqlite.sqlite3.connect",
                       side_effect=spy_connect), \
                 patch("ihealth.publishers.sqlite._upsert_sql",
                       return_value="INSERT INTO bogus_table_does_not_exist VALUES (?)"):
                with self.assertRaises(SQLitePublishError):
                    pub.publish(DailyHealthData(date=date(2026, 4, 22), step_count=1))

            self.assertEqual(len(spies), 1)
            self.assertTrue(spies[0].rollback_called, "rollback() が呼ばれていない")
            self.assertFalse(spies[0].commit_called, "commit() が呼ばれてしまった")

    def test_commit_called_on_success(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "spy_ok.db"
            spies: "list[_ConnSpy]" = []
            real_connect = sqlite3.connect

            def spy_connect(*args, **kwargs):
                spy = _ConnSpy(real_connect(*args, **kwargs))
                spies.append(spy)
                return spy

            pub = SQLitePublisher(
                db_path=db_path, target_date=date(2026, 4, 22),
                logger=MagicMock(),
            )
            with patch("ihealth.publishers.sqlite.sqlite3.connect",
                       side_effect=spy_connect):
                pub.publish(DailyHealthData(date=date(2026, 4, 22), step_count=1))

            self.assertEqual(len(spies), 1)
            self.assertTrue(spies[0].commit_called)
            self.assertFalse(spies[0].rollback_called)


class TestSQLiteSatisfiesProtocol(unittest.TestCase):
    def test_publish_callable(self):
        from ihealth.workflow import Publisher
        with tempfile.TemporaryDirectory() as tmp:
            pub = SQLitePublisher(
                db_path=Path(tmp) / "x.db",
                target_date=date(2026, 4, 22),
                logger=_logger(),
            )
            self.assertTrue(callable(pub.publish))
            self.assertTrue(hasattr(Publisher, "__name__"))


if __name__ == "__main__":
    unittest.main()
