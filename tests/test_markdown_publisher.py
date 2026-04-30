"""Markdown Publisher (Phase 1 A3-md) の回帰テスト.

外部 I/O は ``tempfile.TemporaryDirectory`` で sandbox に隔離.
HTTP も Notion も触らない.
"""

from __future__ import annotations

import logging
import math
import multiprocessing as _mp
import os
import re as _re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from ihealth.models import DailyHealthData
from ihealth.publishers.markdown import (
    MARKDOWN_FIELD_SPECS,
    MarkdownPublishError,
    MarkdownPublisher,
    _coerce_float,
    _coerce_int,
    _convert_field,
    _format_scalar,
    render_body,
    render_frontmatter,
)


def _logger() -> logging.Logger:
    return logging.getLogger("test-markdown-publisher")


#: codex round 3 指摘 (2026-04-29): worker ごとに **構造的に違う** payload にして
#: 「壊れた共有 tmp 実装でも 1 桁差なら通る」抜けを潰す. 8 worker 各々で
#: 異なるフィールド集合を持たせ、最終ファイルが 8 通りの期待 content の集合に
#: 必ず属する (= byte 単位で完全一致する) ことを assert できる.
_WORKER_CASES: "list[dict[str, object]]" = [
    {"step_count": 1},
    {"step_count": 22222, "sleep_hours": 7.25},
    {"step_count": 333, "distance_km": 12.34, "body_mass_kg": 61.2},
    {"step_count": 4444, "mindful_sessions": 2, "mindful_minutes": 15.5},
    {"step_count": 55555, "oxygen_saturation": 97.12},
    {"step_count": 666, "active_energy_kcal": 543.2, "heart_rate_avg": 71.0},
    {"step_count": 7777, "heart_rate_max": 155.0, "heart_rate_resting": 49.0},
    {"step_count": 88888, "nap_hours": 0.75, "body_fat_percentage": 18.4},
]


def _publish_worker(out_dir: str, target_iso: str, case_index: int) -> None:
    """multiprocessing worker: 別プロセスで MarkdownPublisher.publish を実行する.

    module top-level に置く必要あり (multiprocessing が pickle するため).
    codex round 2 / 3 指摘 (2026-04-29) で本物の race condition を検証する用.
    """
    target = date.fromisoformat(target_iso)
    data = DailyHealthData(date=target, **_WORKER_CASES[case_index])  # type: ignore[arg-type]
    pub = MarkdownPublisher(
        Path(out_dir), target, logging.getLogger(f"worker-{case_index}"),
    )
    pub.publish(data)


class TestFormatScalar(unittest.TestCase):
    """``_format_scalar`` の YAML scalar 化."""

    def test_int(self):
        self.assertEqual(_format_scalar(42), "42")

    def test_negative_int(self):
        self.assertEqual(_format_scalar(-7), "-7")

    def test_zero_int(self):
        self.assertEqual(_format_scalar(0), "0")

    def test_float_basic(self):
        self.assertEqual(_format_scalar(5.2), "5.2")

    def test_float_negative(self):
        self.assertEqual(_format_scalar(-1.5), "-1.5")

    def test_float_zero(self):
        # repr(0.0) == "0.0" — YAML number として有効
        self.assertEqual(_format_scalar(0.0), "0.0")

    def test_float_nan_raises(self):
        with self.assertRaises(MarkdownPublishError):
            _format_scalar(float("nan"))

    def test_float_inf_raises(self):
        with self.assertRaises(MarkdownPublishError):
            _format_scalar(float("inf"))

    def test_float_negative_inf_raises(self):
        with self.assertRaises(MarkdownPublishError):
            _format_scalar(float("-inf"))

    def test_bool_rejected(self):
        # bool は int の subclass だが scalar として emit しない
        with self.assertRaises(TypeError):
            _format_scalar(True)
        with self.assertRaises(TypeError):
            _format_scalar(False)

    def test_string_rejected(self):
        with self.assertRaises(TypeError):
            _format_scalar("hello")

    def test_list_rejected(self):
        with self.assertRaises(TypeError):
            _format_scalar([1, 2, 3])

    def test_none_rejected(self):
        with self.assertRaises(TypeError):
            _format_scalar(None)


class TestFieldSpecsValidation(unittest.TestCase):
    """``MARKDOWN_FIELD_SPECS`` が ``DailyHealthData`` のフィールドと 1:1."""

    def test_all_fields_covered(self):
        from dataclasses import fields as dataclass_fields
        model_fields = {f.name for f in dataclass_fields(DailyHealthData)} - {"date"}
        spec_fields = set(MARKDOWN_FIELD_SPECS.keys())
        self.assertEqual(model_fields, spec_fields)


class TestCoerceInt(unittest.TestCase):
    """``_coerce_int``: codex round 1 指摘の silent coercion 対策."""

    def test_int_passthrough(self):
        self.assertEqual(_coerce_int("step_count", 12345), 12345)
        self.assertEqual(_coerce_int("step_count", 0), 0)
        self.assertEqual(_coerce_int("step_count", -1), -1)

    def test_bool_rejected(self):
        # bool は int subclass だが silent に 1/0 にしない (codex round 1 blocking)
        with self.assertRaises(MarkdownPublishError) as ctx:
            _coerce_int("step_count", True)
        self.assertIn("step_count", str(ctx.exception))
        with self.assertRaises(MarkdownPublishError):
            _coerce_int("step_count", False)

    def test_float_rejected(self):
        # int(1.9) → 1 のような silent 切り捨てを防ぐ (codex round 1 blocking)
        with self.assertRaises(MarkdownPublishError) as ctx:
            _coerce_int("step_count", 1.9)
        self.assertIn("step_count", str(ctx.exception))

    def test_int_valued_float_still_rejected(self):
        # 5.0 も型が float なので reject (型システムで step_count は int 限定)
        with self.assertRaises(MarkdownPublishError):
            _coerce_int("step_count", 5.0)

    def test_str_rejected(self):
        with self.assertRaises(MarkdownPublishError):
            _coerce_int("step_count", "5")

    def test_none_rejected(self):
        # None は呼び出し側でフィルタするが、念のため
        with self.assertRaises(MarkdownPublishError):
            _coerce_int("step_count", None)


class TestCoerceFloat(unittest.TestCase):
    """``_coerce_float``: codex round 1 指摘の silent coercion 対策."""

    def test_float_passthrough(self):
        self.assertEqual(_coerce_float("distance_km", 5.234, 2), 5.23)

    def test_int_promoted_to_float(self):
        # int → float は意図的に許可 (例: heart_rate=70)
        self.assertEqual(_coerce_float("heart_rate_avg", 70, 1), 70.0)

    def test_bool_rejected(self):
        with self.assertRaises(MarkdownPublishError) as ctx:
            _coerce_float("distance_km", True, 2)
        self.assertIn("distance_km", str(ctx.exception))
        self.assertIn("bool", str(ctx.exception))

    def test_str_rejected(self):
        with self.assertRaises(MarkdownPublishError):
            _coerce_float("distance_km", "5.0", 2)

    def test_nan_rejected(self):
        with self.assertRaises(MarkdownPublishError) as ctx:
            _coerce_float("distance_km", float("nan"), 2)
        self.assertIn("NaN/Inf", str(ctx.exception))

    def test_inf_rejected(self):
        with self.assertRaises(MarkdownPublishError):
            _coerce_float("distance_km", float("inf"), 2)
        with self.assertRaises(MarkdownPublishError):
            _coerce_float("distance_km", float("-inf"), 2)

    def test_rounding(self):
        # banker's rounding は許容 (Python 標準の round)
        self.assertEqual(_coerce_float("x", 5.235, 2), round(5.235, 2))
        self.assertEqual(_coerce_float("x", -1.234, 1), -1.2)


class TestConvertField(unittest.TestCase):
    """``_convert_field``: spec.kind ごとの dispatch."""

    def test_int_field_uses_coerce_int(self):
        spec = MARKDOWN_FIELD_SPECS["step_count"]
        self.assertEqual(_convert_field("step_count", 100, spec), 100)
        with self.assertRaises(MarkdownPublishError):
            _convert_field("step_count", 1.5, spec)

    def test_float_field_uses_coerce_float(self):
        spec = MARKDOWN_FIELD_SPECS["distance_km"]
        self.assertEqual(_convert_field("distance_km", 5.234, spec), 5.23)
        with self.assertRaises(MarkdownPublishError):
            _convert_field("distance_km", "5.0", spec)


class TestRenderFrontmatter(unittest.TestCase):
    """frontmatter のレンダリング."""

    def test_only_date(self):
        data = DailyHealthData(date=date(2026, 4, 22))
        result = render_frontmatter(data)
        self.assertEqual(result, "---\ndate: 2026-04-22\n---\n")

    def test_step_count_only(self):
        data = DailyHealthData(date=date(2026, 4, 22), step_count=12345)
        result = render_frontmatter(data)
        self.assertEqual(
            result,
            "---\ndate: 2026-04-22\nstep_count: 12345\n---\n",
        )

    def test_multiple_fields(self):
        data = DailyHealthData(
            date=date(2026, 4, 22),
            step_count=12345,
            distance_km=5.234,
            sleep_hours=7.234,
        )
        result = render_frontmatter(data)
        # フィールド順は MARKDOWN_FORMATTERS の定義順 (date 先頭)
        expected_lines = [
            "---",
            "date: 2026-04-22",
            "step_count: 12345",
            "distance_km: 5.23",       # round(5.234, 2)
            "sleep_hours: 7.23",       # round(7.234, 2)
            "---",
            "",  # 末尾 \n
        ]
        self.assertEqual(result, "\n".join(expected_lines))

    def test_none_fields_skipped(self):
        data = DailyHealthData(
            date=date(2026, 4, 22),
            step_count=12345,
            # distance_km / sleep_hours / 等は None
        )
        result = render_frontmatter(data)
        self.assertNotIn("distance_km", result)
        self.assertNotIn("sleep_hours", result)
        self.assertNotIn("body_mass_kg", result)
        self.assertIn("step_count: 12345", result)

    def test_field_order_preserved(self):
        # MARKDOWN_FORMATTERS の定義順 (= ほぼ DailyHealthData 宣言順) で並ぶ
        data = DailyHealthData(
            date=date(2026, 4, 22),
            body_mass_kg=65.5,
            step_count=10000,  # MARKDOWN_FORMATTERS では先頭に近い
        )
        result = render_frontmatter(data)
        idx_step = result.index("step_count")
        idx_body = result.index("body_mass_kg")
        self.assertLess(idx_step, idx_body, "step_count should appear before body_mass_kg")

    def test_int_step_count_converted(self):
        data = DailyHealthData(date=date(2026, 4, 22), step_count=12345)
        result = render_frontmatter(data)
        # int(12345) なので小数点は出ない
        self.assertIn("step_count: 12345\n", result)
        self.assertNotIn("12345.", result)

    def test_distance_rounding(self):
        data = DailyHealthData(date=date(2026, 4, 22), distance_km=5.236)
        result = render_frontmatter(data)
        # round(5.236, 2) == 5.24
        self.assertIn("distance_km: 5.24\n", result)

    def test_sleep_hours_unit_preserved(self):
        # Notion publisher は分換算するが Markdown は時間そのまま
        data = DailyHealthData(date=date(2026, 4, 22), sleep_hours=7.5)
        result = render_frontmatter(data)
        self.assertIn("sleep_hours: 7.5\n", result)
        # 分単位 (450) で書かれていない
        self.assertNotIn("450", result)

    def test_starts_with_fence_and_ends_with_fence(self):
        data = DailyHealthData(date=date(2026, 4, 22), step_count=100)
        result = render_frontmatter(data)
        self.assertTrue(result.startswith("---\n"))
        # 末尾は ``---\n``
        self.assertTrue(result.endswith("---\n"))
        # 開閉 fence が 2 個ぴったり
        self.assertEqual(result.count("---\n"), 2)


class TestRenderBody(unittest.TestCase):
    def test_body_contains_h1_with_date(self):
        data = DailyHealthData(date=date(2026, 4, 22))
        result = render_body(data)
        self.assertIn("# 2026-04-22", result)
        self.assertTrue(result.endswith("\n"))


class TestMarkdownPublisher(unittest.TestCase):
    """publish() の I/O 挙動."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name) / "obsidian-health"
        self.target_date = date(2026, 4, 22)
        self.log = MagicMock()
        self.pub = MarkdownPublisher(
            output_dir=self.out_dir,
            target_date=self.target_date,
            logger=self.log,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_output_dir_if_absent(self):
        # out_dir は setUp 時点では作っていない (TemporaryDirectory の subdir)
        self.assertFalse(self.out_dir.exists())
        data = DailyHealthData(date=self.target_date, step_count=100)
        self.pub.publish(data)
        self.assertTrue(self.out_dir.is_dir())

    def test_writes_file_at_expected_path(self):
        data = DailyHealthData(date=self.target_date, step_count=100)
        self.pub.publish(data)
        expected = self.out_dir / "2026-04-22.md"
        self.assertTrue(expected.is_file())

    def test_file_content_has_frontmatter_and_body(self):
        data = DailyHealthData(date=self.target_date, step_count=100, sleep_hours=7.5)
        self.pub.publish(data)
        path = self.out_dir / "2026-04-22.md"
        content = path.read_text(encoding="utf-8")
        self.assertIn("---\ndate: 2026-04-22", content)
        self.assertIn("step_count: 100", content)
        self.assertIn("sleep_hours: 7.5", content)
        self.assertIn("# 2026-04-22 健康データ", content)

    def test_overwrites_existing_file(self):
        # 既存ファイルを置く
        self.out_dir.mkdir(parents=True)
        path = self.out_dir / "2026-04-22.md"
        path.write_text("OLD CONTENT", encoding="utf-8")

        data = DailyHealthData(date=self.target_date, step_count=999)
        self.pub.publish(data)
        content = path.read_text(encoding="utf-8")
        self.assertNotIn("OLD CONTENT", content)
        self.assertIn("step_count: 999", content)

    def test_target_date_mismatch_raises(self):
        # health_data.date が publisher の target_date と違うと raise
        wrong_data = DailyHealthData(date=date(2026, 4, 23), step_count=100)
        with self.assertRaises(MarkdownPublishError) as ctx:
            self.pub.publish(wrong_data)
        self.assertIn("target_date 不整合", str(ctx.exception))

    def test_logs_success_with_populated_count(self):
        data = DailyHealthData(
            date=self.target_date,
            step_count=100,
            distance_km=5.0,
            sleep_hours=7.0,
        )
        self.pub.publish(data)
        # logger.info が呼ばれていて populated=3 を含む
        self.assertTrue(self.log.info.called)
        last_call_msg = self.log.info.call_args[0][0]
        last_call_args = self.log.info.call_args[0][1:]
        self.assertIn("populated=%d", last_call_msg)
        # 3 件 populated (step_count / distance_km / sleep_hours)
        self.assertIn(3, last_call_args)

    def test_atomic_write_no_tmp_file_left(self):
        data = DailyHealthData(date=self.target_date, step_count=100)
        self.pub.publish(data)
        # 成功したら .tmp は残らない
        tmp_files = list(self.out_dir.glob("*.tmp"))
        self.assertEqual(tmp_files, [])

    def test_utf8_japanese_in_body(self):
        # body には日本語 ("健康データ") が入る → utf-8 で正しく write されること
        data = DailyHealthData(date=self.target_date, step_count=100)
        self.pub.publish(data)
        path = self.out_dir / "2026-04-22.md"
        # bytes 比較で utf-8 化確認
        raw = path.read_bytes()
        self.assertIn("健康データ".encode("utf-8"), raw)

    def test_output_dir_is_a_file_raises(self):
        # output_dir のパスが既存の **ファイル** だった場合 mkdir で失敗
        broken = Path(self._tmp.name) / "actually_a_file"
        broken.write_text("x", encoding="utf-8")
        pub = MarkdownPublisher(
            output_dir=broken,
            target_date=self.target_date,
            logger=self.log,
        )
        data = DailyHealthData(date=self.target_date, step_count=100)
        with self.assertRaises(MarkdownPublishError) as ctx:
            pub.publish(data)
        self.assertIn("出力ディレクトリ作成に失敗", str(ctx.exception))

    def test_only_date_data_writes_minimal_file(self):
        # 全フィールド None でも date だけで書ける (= 全欠落でも publish は成功)
        data = DailyHealthData(date=self.target_date)
        self.pub.publish(data)
        path = self.out_dir / "2026-04-22.md"
        content = path.read_text(encoding="utf-8")
        # frontmatter は date のみ
        self.assertIn("---\ndate: 2026-04-22\n---\n", content)
        # 他のフィールドは出ない
        for field_name in MARKDOWN_FIELD_SPECS:
            self.assertNotIn(f"{field_name}:", content)


class TestRenderFrontmatterRejectsBadInputs(unittest.TestCase):
    """``render_frontmatter`` が ``DailyHealthData`` の不正値で fail-fast.

    codex round 1 (2026-04-29) blocking #3 の回帰テスト. 旧実装では ``int(True)``
    / ``int(1.9)`` が silent に通っていた.
    """

    def test_step_count_bool_rejected(self):
        # bool は range 検証 (DailyHealthData.__post_init__) を通る
        # (True == 1 で step_count >= 0 を満たす) ので Markdown 層で弾く必要がある.
        data = DailyHealthData.__new__(DailyHealthData)
        # frozen=True でも __new__ + object.__setattr__ で組める
        object.__setattr__(data, "date", date(2026, 4, 22))
        for f in MARKDOWN_FIELD_SPECS:
            object.__setattr__(data, f, None)
        object.__setattr__(data, "step_count", True)
        with self.assertRaises(MarkdownPublishError) as ctx:
            render_frontmatter(data)
        self.assertIn("step_count", str(ctx.exception))

    def test_step_count_float_rejected(self):
        data = DailyHealthData.__new__(DailyHealthData)
        object.__setattr__(data, "date", date(2026, 4, 22))
        for f in MARKDOWN_FIELD_SPECS:
            object.__setattr__(data, f, None)
        object.__setattr__(data, "step_count", 1.9)
        with self.assertRaises(MarkdownPublishError) as ctx:
            render_frontmatter(data)
        self.assertIn("step_count", str(ctx.exception))


class TestConcurrentWrite(unittest.TestCase):
    """codex round 1 blocking #1 の回帰テスト: 同時実行で破壊しない.

    別プロセスではなく **多重に publisher を呼ぶ** テストで unique tmp 機構
    そのものを検証する (multiprocessing は CI 環境差で flaky になりうるので
    threading で回す + atomic semantics の確認).
    """

    def test_serialized_writes_no_corruption(self):
        # 連続 publish で最終ファイルがどちらかの完全な内容になる (混在しない).
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            log = MagicMock()
            target = date(2026, 4, 22)
            for i in range(20):
                pub = MarkdownPublisher(out, target, log)
                data = DailyHealthData(date=target, step_count=i)
                pub.publish(data)
            content = (out / "2026-04-22.md").read_text(encoding="utf-8")
            # 最終内容は最後の publish (step_count=19)
            self.assertIn("step_count: 19", content)
            # tmp ファイルは残らない (成功時)
            self.assertEqual(list(out.glob("*.tmp")), [])
            # hidden tmp prefix も残らない
            self.assertEqual(list(out.glob(".*.tmp")), [])

    @unittest.skipIf(sys.platform == "win32", "fork unavailable on Windows")
    def test_multiprocess_no_corruption(self):
        """codex round 2 / 3 指摘の本格的並行テスト.

        複数 fork プロセスから同じ日付ファイルを同時 publish しても、最終
        ファイルが **単一 worker の完全コンテンツ (byte 単位一致)** になる
        ことを確認する.

        round 3 指摘対応:
        - worker ごとに構造的に違う payload (フィールド集合が異なる) にする
          ことで「shared tmp で内容が混じっても 1 桁差なら通る」抜けを潰す.
        - 期待 content を ``render_frontmatter() + "\\n" + render_body()`` で
          生成し、最終ファイルが 8 通りの期待集合に完全一致することを assert.
        - timeout 時に ``terminate()`` で子プロセスを回収.
        """
        target = date(2026, 4, 22)
        # 期待される content は 8 worker 各々の "完全な" 出力
        expected_contents = {
            (
                render_frontmatter(DailyHealthData(date=target, **case))  # type: ignore[arg-type]
                + "\n"
                + render_body(DailyHealthData(date=target, **case))  # type: ignore[arg-type]
            )
            for case in _WORKER_CASES
        }

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            ctx = _mp.get_context("fork")
            num_workers = len(_WORKER_CASES)
            procs = [
                ctx.Process(
                    target=_publish_worker,
                    args=(str(out), target.isoformat(), i),
                )
                for i in range(num_workers)
            ]
            for p in procs:
                p.start()
            try:
                for p in procs:
                    p.join(timeout=30)
                    if p.exitcode is None:
                        p.terminate()
                        p.join(timeout=5)
                        self.fail(f"worker hung: pid={p.pid}")
                    self.assertEqual(
                        p.exitcode, 0, f"worker exit={p.exitcode}",
                    )
            finally:
                # 念のため後始末: まだ残ってるプロセスは terminate
                for p in procs:
                    if p.is_alive():
                        p.terminate()
                        p.join(timeout=5)

            content = (out / "2026-04-22.md").read_text(encoding="utf-8")
            # 完全一致: 最終ファイルは 8 通りの期待 content の **どれか 1 つ**
            self.assertIn(
                content, expected_contents,
                "並行書き込みで content が期待集合外 (= 内容が混在した可能性)",
            )
            # tmp 残骸が無い
            self.assertEqual(list(out.glob("*.tmp")), [])
            self.assertEqual(list(out.glob(".*.tmp")), [])

    def test_unique_tmp_file_naming(self):
        # 同じ日付ファイルを **2 回続けて publish** する間に tmp 名が衝突
        # しないことを mock で確認 (mkstemp が呼ばれるたびに違う path が返る).
        import unittest.mock
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            log = MagicMock()
            target = date(2026, 4, 22)
            pub = MarkdownPublisher(out, target, log)
            data = DailyHealthData(date=target, step_count=1)
            # mkstemp の戻り値 (fd, name) を観察
            real_mkstemp = tempfile.mkstemp
            seen_names: "list[str]" = []

            def spy(*args, **kwargs):
                fd, name = real_mkstemp(*args, **kwargs)
                seen_names.append(name)
                return fd, name

            with unittest.mock.patch("tempfile.mkstemp", side_effect=spy):
                pub.publish(data)
                pub.publish(data)
            self.assertEqual(len(seen_names), 2)
            self.assertNotEqual(seen_names[0], seen_names[1])


class TestMarkdownPublisherSatisfiesProtocol(unittest.TestCase):
    """``MarkdownPublisher`` が :class:`Publisher` プロトコルを満たすことを確認."""

    def test_callable_publish_signature(self):
        from ihealth.workflow import Publisher
        with tempfile.TemporaryDirectory() as tmp:
            pub = MarkdownPublisher(
                output_dir=Path(tmp) / "out",
                target_date=date(2026, 4, 22),
                logger=_logger(),
            )
            # Protocol は structural なので isinstance では判定できないが、
            # publish(health_data) が呼べることだけ確認 (= 実装契約).
            self.assertTrue(callable(pub.publish))
            data = DailyHealthData(date=date(2026, 4, 22), step_count=1)
            pub.publish(data)  # raise しないこと
            # Publisher Protocol が import できることも確認
            self.assertTrue(hasattr(Publisher, "__name__"))


if __name__ == "__main__":
    unittest.main()
