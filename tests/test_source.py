"""source.py (fetch_all_metrics / _fetch_once / decompress_hae) の回帰テスト。

unittest + unittest.mock.patch で subprocess / COMPRESSION_TOOL をモック。
テスト環境は macOS 以外 (Linux CI) でも動くように、**COMPRESSION_TOOL を実在する
パス (/usr/bin/env など) に差し替えて COMPRESSION_TOOL.exists() を通す** 運用。
実際の呼び出しは subprocess.run のモックで受け止めるので、実行ファイルが
何であっても影響しない。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from ihealth.source import (
    FetchResult,
    METRIC_DIRS,
    SourceError,
    _CRITICAL_METRICS,
    _SF_DATALESS,
    _ensure_materialized,
    _fetch_once,
    _is_dataless,
    _retry_settings_from_env,
    decompress_hae,
    fetch_all_metrics,
)

#: COMPRESSION_TOOL パッチ先。``Path.exists()`` が True になれば decompress_hae の
#: 事前条件チェックを通せる。``/usr/bin/env`` は macOS / Linux 両方に存在する。
_FAKE_COMPRESSION_TOOL = Path("/usr/bin/env")


def _build_autosync_layout(root: Path, target_date: date, with_metrics: "set[str] | None" = None) -> None:
    """AutoSync/HealthMetrics 配下に .hae ダミーファイルを配置する。

    ``with_metrics`` を省略すると全 11 ディレクトリ分を作成。
    実ファイルはダミー (空 bytes) で、compression_tool のモックが上書きで
    JSON を書き出す前提。
    """
    hm = root / "HealthMetrics"
    hm.mkdir(parents=True, exist_ok=True)
    stem = target_date.strftime("%Y%m%d") + ".hae"
    dirs = with_metrics if with_metrics is not None else set(METRIC_DIRS.values())
    for d in METRIC_DIRS.values():
        (hm / d).mkdir(parents=True, exist_ok=True)
        if d in dirs:
            (hm / d / stem).write_bytes(b"fake-lzfse")


def _fake_compression_tool_writes_json(*args, **kwargs):
    """subprocess.run のモック: `-o <dst>` 引数に合わせて空の JSON を書き出す。

    returncode=0 のサンプルを返す (解凍成功シミュレーション)。
    """
    # args[0] が command list
    cmd = args[0]
    # "-o" の次が出力先
    dst = None
    for i, token in enumerate(cmd):
        if token == "-o" and i + 1 < len(cmd):
            dst = Path(cmd[i + 1])
            break
    if dst is not None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(
            json.dumps({"metric": "FakeMetric", "data": []}, ensure_ascii=False),
            encoding="utf-8",
        )

    class _FakeCompleted:
        returncode = 0
        stdout = b""
        stderr = b""

    return _FakeCompleted()


def _fake_compression_tool_failure(*args, **kwargs):
    """compression_tool が returncode=1 を返す失敗シミュレーション。"""
    class _FakeFailed:
        returncode = 1
        stdout = b""
        stderr = b"decompress failed"

    return _FakeFailed()


class TestFetchOnce(unittest.TestCase):
    """_fetch_once の基本動作 (METRIC_DIRS 巡回・archive_dir 作成・missing 収集)"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.target_date = date(2026, 4, 22)
        self.archive_root = self.root / "archive"
        # COMPRESSION_TOOL.exists() を Linux CI でも通す: どの OS でも実在する
        # /usr/bin/env に差し替える (実行は subprocess.run モックで受け止める)
        self._ctool_patcher = patch("ihealth.source.COMPRESSION_TOOL", _FAKE_COMPRESSION_TOOL)
        self._ctool_patcher.start()

    def tearDown(self):
        self._ctool_patcher.stop()
        self._tmp.cleanup()

    def test_all_metrics_present_archives_all(self):
        _build_autosync_layout(self.root / "AutoSync", self.target_date)
        import logging
        logger = logging.getLogger("test-fetch-once")
        with patch("ihealth.source.subprocess.run", side_effect=_fake_compression_tool_writes_json):
            result = _fetch_once(self.root / "AutoSync", self.target_date, self.archive_root, logger)
        self.assertIsInstance(result, FetchResult)
        # 11 ディレクトリ全部から解凍済み
        self.assertEqual(len(result.archived), len(METRIC_DIRS))
        self.assertEqual(result.missing, [])

    def test_missing_hae_goes_to_missing_list(self):
        # mindful_minutes だけ欠落
        present = set(METRIC_DIRS.values()) - {"mindful_minutes"}
        _build_autosync_layout(self.root / "AutoSync", self.target_date, with_metrics=present)
        import logging
        logger = logging.getLogger("test-missing")
        with patch("ihealth.source.subprocess.run", side_effect=_fake_compression_tool_writes_json):
            result = _fetch_once(self.root / "AutoSync", self.target_date, self.archive_root, logger)
        self.assertIn("mindful_minutes", result.missing)
        self.assertNotIn("mindful_minutes", result.archived)

    def test_subprocess_failure_marks_missing(self):
        _build_autosync_layout(self.root / "AutoSync", self.target_date)
        import logging
        logger = logging.getLogger("test-sp-failure")
        with patch("ihealth.source.subprocess.run", side_effect=_fake_compression_tool_failure):
            result = _fetch_once(self.root / "AutoSync", self.target_date, self.archive_root, logger)
        # 全部 compression_tool が失敗 → 全部 missing
        self.assertEqual(len(result.archived), 0)
        self.assertEqual(sorted(result.missing), sorted(METRIC_DIRS.keys()))

    def test_archive_dir_is_created_with_date(self):
        _build_autosync_layout(self.root / "AutoSync", self.target_date)
        import logging
        logger = logging.getLogger("test-archive-dir")
        with patch("ihealth.source.subprocess.run", side_effect=_fake_compression_tool_writes_json):
            result = _fetch_once(self.root / "AutoSync", self.target_date, self.archive_root, logger)
        self.assertTrue(result.archive_dir.is_dir())
        self.assertEqual(result.archive_dir.name, "2026-04-22")


class TestFetchAllMetricsRetry(unittest.TestCase):
    """fetch_all_metrics の retry ロジック"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.target_date = date(2026, 4, 22)
        self.archive_root = self.root / "archive"
        # テスト高速化のため retry を 0 秒 × 1 回に
        self._prev_env = {
            "IHEALTH_RETRY_WAIT_SEC": os.environ.pop("IHEALTH_RETRY_WAIT_SEC", None),
            "IHEALTH_RETRY_MAX": os.environ.pop("IHEALTH_RETRY_MAX", None),
        }
        os.environ["IHEALTH_RETRY_WAIT_SEC"] = "0"
        os.environ["IHEALTH_RETRY_MAX"] = "1"
        # COMPRESSION_TOOL を OS 非依存の存在パスに差し替え (Linux CI 対応)
        self._ctool_patcher = patch("ihealth.source.COMPRESSION_TOOL", _FAKE_COMPRESSION_TOOL)
        self._ctool_patcher.start()

    def tearDown(self):
        self._ctool_patcher.stop()
        self._tmp.cleanup()
        for k, v in self._prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_no_critical_missing_returns_immediately(self):
        _build_autosync_layout(self.root / "AutoSync", self.target_date)
        with patch("ihealth.source.subprocess.run", side_effect=_fake_compression_tool_writes_json):
            result = fetch_all_metrics(
                self.root / "AutoSync", self.target_date, self.archive_root,
            )
        self.assertEqual(len(result.archived), len(METRIC_DIRS))

    def test_critical_missing_retries_then_raises(self):
        # step_count だけ欠落 (critical)
        present = set(METRIC_DIRS.values()) - {"step_count"}
        _build_autosync_layout(self.root / "AutoSync", self.target_date, with_metrics=present)
        with patch("ihealth.source.subprocess.run", side_effect=_fake_compression_tool_writes_json):
            with self.assertRaises(SourceError) as ctx:
                fetch_all_metrics(
                    self.root / "AutoSync", self.target_date, self.archive_root,
                )
        self.assertIn("主要メトリクス", str(ctx.exception))
        self.assertIn("step_count", str(ctx.exception))

    def test_non_critical_missing_does_not_retry(self):
        # mindful_minutes (非 critical) だけ欠落 → retry せずに成功
        present = set(METRIC_DIRS.values()) - {"mindful_minutes"}
        _build_autosync_layout(self.root / "AutoSync", self.target_date, with_metrics=present)
        with patch("ihealth.source.subprocess.run", side_effect=_fake_compression_tool_writes_json):
            result = fetch_all_metrics(
                self.root / "AutoSync", self.target_date, self.archive_root,
            )
        self.assertIn("mindful_minutes", result.missing)
        # critical は揃っているので成功 (exception なし)

    def test_all_missing_raises_source_error(self):
        # HealthMetrics ディレクトリは存在するが .hae が 1 件もない
        (self.root / "AutoSync" / "HealthMetrics").mkdir(parents=True)
        for d in METRIC_DIRS.values():
            (self.root / "AutoSync" / "HealthMetrics" / d).mkdir()
        with patch("ihealth.source.subprocess.run", side_effect=_fake_compression_tool_writes_json):
            with self.assertRaises(SourceError) as ctx:
                fetch_all_metrics(
                    self.root / "AutoSync", self.target_date, self.archive_root,
                )
        msg = str(ctx.exception)
        # 診断情報 (archived 件数 / attempts / metrics_root) が含まれていること
        self.assertIn("2 回試行", msg)  # 1 + 1 retry

    def test_healthmetrics_dir_missing_raises(self):
        # HealthMetrics ディレクトリそのものが無い
        (self.root / "AutoSync").mkdir()
        with self.assertRaises(SourceError) as ctx:
            fetch_all_metrics(
                self.root / "AutoSync", self.target_date, self.archive_root,
            )
        self.assertIn("HealthMetrics", str(ctx.exception))


class TestRetrySettingsFromEnv(unittest.TestCase):
    def setUp(self):
        self._saved = {
            k: os.environ.pop(k, None)
            for k in ("IHEALTH_RETRY_WAIT_SEC", "IHEALTH_RETRY_MAX")
        }

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_defaults_without_env(self):
        self.assertEqual(_retry_settings_from_env(), (120.0, 5))

    def test_valid_override(self):
        os.environ["IHEALTH_RETRY_WAIT_SEC"] = "10"
        os.environ["IHEALTH_RETRY_MAX"] = "3"
        self.assertEqual(_retry_settings_from_env(), (10.0, 3))

    def test_nan_and_inf_fallback_to_default(self):
        for bad in ("nan", "NaN", "inf", "Infinity", "-inf"):
            os.environ["IHEALTH_RETRY_WAIT_SEC"] = bad
            self.assertEqual(
                _retry_settings_from_env()[0], 120.0,
                f"IHEALTH_RETRY_WAIT_SEC={bad} should fallback",
            )

    def test_negative_fallback(self):
        os.environ["IHEALTH_RETRY_WAIT_SEC"] = "-5"
        self.assertEqual(_retry_settings_from_env()[0], 120.0)
        os.environ["IHEALTH_RETRY_MAX"] = "-1"
        self.assertEqual(_retry_settings_from_env()[1], 5)

    def test_clamp_upper_bound(self):
        os.environ["IHEALTH_RETRY_WAIT_SEC"] = "99999"
        self.assertEqual(_retry_settings_from_env()[0], 3600.0)
        os.environ["IHEALTH_RETRY_WAIT_SEC"] = "0"
        os.environ["IHEALTH_RETRY_MAX"] = "9999"
        # 9999 > 100 → デフォルトに戻る
        self.assertEqual(_retry_settings_from_env()[1], 5)


class TestDecompressHaeErrors(unittest.TestCase):
    """decompress_hae の事前条件違反と失敗モード"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_compression_tool_missing_raises(self):
        src = self.root / "fake.hae"
        src.write_bytes(b"x")
        dst = self.root / "out.json"
        # COMPRESSION_TOOL が存在しない状態をシミュレート
        fake_tool = self.root / "nonexistent_compression_tool"
        with patch("ihealth.source.COMPRESSION_TOOL", fake_tool):
            with self.assertRaises(SourceError) as ctx:
                decompress_hae(src, dst)
        self.assertIn("見つかりません", str(ctx.exception))

    def test_subprocess_returncode_nonzero_raises(self):
        src = self.root / "fake.hae"
        src.write_bytes(b"x")
        dst = self.root / "out.json"
        # COMPRESSION_TOOL 存在チェックを通した上で subprocess の失敗を検証する
        with patch("ihealth.source.COMPRESSION_TOOL", _FAKE_COMPRESSION_TOOL), \
             patch("ihealth.source.subprocess.run", side_effect=_fake_compression_tool_failure):
            with self.assertRaises(SourceError) as ctx:
                decompress_hae(src, dst)
        self.assertIn("compression_tool が失敗", str(ctx.exception))


class TestIsDataless(unittest.TestCase):
    """_is_dataless: SF_DATALESS フラグの判定 (副作用なし)"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "x.hae"
        self.path.write_bytes(b"")

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_true_when_flag_set(self):
        # SF_DATALESS が立っている stat 結果をモック
        fake_stat = unittest.mock.MagicMock()
        fake_stat.st_flags = _SF_DATALESS | 0x20  # 他のフラグと混在しても拾える
        with patch("ihealth.source.os.stat", return_value=fake_stat):
            self.assertTrue(_is_dataless(self.path))

    def test_returns_false_when_flag_unset(self):
        fake_stat = unittest.mock.MagicMock()
        fake_stat.st_flags = 0  # まったくフラグなし
        with patch("ihealth.source.os.stat", return_value=fake_stat):
            self.assertFalse(_is_dataless(self.path))

    def test_returns_false_on_oserror(self):
        # stat 自体が失敗 (path 消えた等) → False で通常フローへ戻す
        with patch("ihealth.source.os.stat", side_effect=OSError("no such file")):
            self.assertFalse(_is_dataless(self.path))

    def test_returns_false_when_st_flags_missing(self):
        # 非 BSD 系で st_flags 属性がない場合 (Linux CI) も False
        fake_stat = unittest.mock.MagicMock(spec=[])  # 属性ゼロ
        with patch("ihealth.source.os.stat", return_value=fake_stat):
            self.assertFalse(_is_dataless(self.path))


class TestEnsureMaterialized(unittest.TestCase):
    """_ensure_materialized: dataless なら brctl で実体化を要求し、polling で待つ"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "x.hae"
        self.path.write_bytes(b"")
        import logging
        self.log = logging.getLogger("test-ensure-materialized")
        # polling を高速化 (テストが現実時間で待たないように)
        self._poll_patcher = patch(
            "ihealth.source._MATERIALIZE_POLL_INTERVAL_SEC", 0.0,
        )
        self._timeout_patcher = patch(
            "ihealth.source._MATERIALIZE_TIMEOUT_SEC", 0.5,
        )
        self._poll_patcher.start()
        self._timeout_patcher.start()

    def tearDown(self):
        self._timeout_patcher.stop()
        self._poll_patcher.stop()
        self._tmp.cleanup()

    def _fake_brctl_ok(self, *args, **kwargs):
        class _R:
            returncode = 0
            stdout = b""
            stderr = b""
        return _R()

    def _fake_brctl_fail(self, *args, **kwargs):
        class _R:
            returncode = 1
            stdout = b""
            stderr = b"brctl: failed"
        return _R()

    def test_no_op_when_not_dataless(self):
        # dataless でないファイルは brctl を呼ばない (高速パス)
        with patch("ihealth.source._is_dataless", return_value=False), \
             patch("ihealth.source.subprocess.run") as mock_run:
            _ensure_materialized(self.path, self.log)
        mock_run.assert_not_called()

    @staticmethod
    def _stat_with_flags(*flag_sequence: int):
        """polling テスト用の os.stat ヘルパー: 呼ばれるたびに次の st_flags を返す.

        終端を超えて呼ばれた場合は最後の値を繰り返す (タイムアウトテスト等).
        codex round 1 PR #27 指摘 (2026-04-29): Python 3.11+ の pathlib は
        ``os.stat(..., follow_symlinks=...)`` を呼ぶので、 mock 関数も
        keyword 引数を吸収する必要がある.
        """
        states = list(flag_sequence)

        def _fake_stat(path, *args, **kwargs):
            value = states.pop(0) if len(states) > 1 else states[0]
            fake = unittest.mock.MagicMock()
            fake.st_flags = value
            return fake
        return _fake_stat

    def test_brctl_called_then_polling_succeeds(self):
        # 関数頭部判定: dataless / polling 1 回目: 未解除 / 2 回目: 解除
        # brctl パスは実在する _FAKE_COMPRESSION_TOOL に流用 (Path.exists 通過のため)
        with patch("ihealth.source._BRCTL", _FAKE_COMPRESSION_TOOL), \
             patch("ihealth.source._is_dataless", return_value=True), \
             patch("ihealth.source.os.stat", side_effect=self._stat_with_flags(_SF_DATALESS, 0)), \
             patch("ihealth.source.subprocess.run", side_effect=self._fake_brctl_ok) as mock_run:
            _ensure_materialized(self.path, self.log)
        # subprocess.run が brctl download で 1 回だけ呼ばれた
        self.assertEqual(mock_run.call_count, 1)
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[1], "download")

    def test_brctl_missing_raises(self):
        fake_missing = Path(self._tmp.name) / "no_such_brctl"
        with patch("ihealth.source._BRCTL", fake_missing), \
             patch("ihealth.source._is_dataless", return_value=True):
            with self.assertRaises(SourceError) as ctx:
                _ensure_materialized(self.path, self.log)
        self.assertIn("見つかりません", str(ctx.exception))

    def test_brctl_returncode_nonzero_raises(self):
        with patch("ihealth.source._BRCTL", _FAKE_COMPRESSION_TOOL), \
             patch("ihealth.source._is_dataless", return_value=True), \
             patch("ihealth.source.subprocess.run", side_effect=self._fake_brctl_fail):
            with self.assertRaises(SourceError) as ctx:
                _ensure_materialized(self.path, self.log)
        self.assertIn("brctl download が失敗", str(ctx.exception))

    def test_brctl_timeout_raises(self):
        """subprocess.run が TimeoutExpired を投げたら SourceError に包まれる"""
        def _timeout_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(
                cmd=args[0], timeout=kwargs.get("timeout", 0.0),
            )
        with patch("ihealth.source._BRCTL", _FAKE_COMPRESSION_TOOL), \
             patch("ihealth.source._is_dataless", return_value=True), \
             patch("ihealth.source.subprocess.run", side_effect=_timeout_run):
            with self.assertRaises(SourceError) as ctx:
                _ensure_materialized(self.path, self.log)
        self.assertIn("brctl download がタイムアウト", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, subprocess.TimeoutExpired)

    def test_brctl_oserror_raises(self):
        """subprocess.run が OSError (PermissionError 等) を投げたら SourceError に包まれる。

        launchd 経由で TCC が deny したケースなど、_BRCTL.exists() を通過しても
        execve 段階で失敗しうる。codex review 2026-04-28 で指摘された経路。
        """
        with patch("ihealth.source._BRCTL", _FAKE_COMPRESSION_TOOL), \
             patch("ihealth.source._is_dataless", return_value=True), \
             patch(
                 "ihealth.source.subprocess.run",
                 side_effect=PermissionError("operation not permitted"),
             ):
            with self.assertRaises(SourceError) as ctx:
                _ensure_materialized(self.path, self.log)
        self.assertIn("brctl download を起動できません", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, PermissionError)

    def test_polling_stat_oserror_raises(self):
        """polling 中に os.stat が OSError を投げたら SourceError に包まれる.

        旧実装では _is_dataless 経由で False が返り「materialize 完了」と
        誤判定していた. codex review 2026-04-28 で指摘された経路.

        Python 3.11+ 互換 (codex round 1 PR #27): pathlib.Path.exists() が
        os.stat(..., follow_symlinks=...) を呼ぶようになったため、
        ``_BRCTL.exists()`` の判定がこの patch にも当たってしまう.
        target ファイルへの呼び出しに限って raise する selective mock に変更.
        """
        target_path = self.path

        def selective_stat(path, *args, **kwargs):
            # str / Path / int(fd) いずれでも target_path 比較できるよう正規化.
            try:
                if Path(str(path)) == target_path:
                    raise OSError("ENOENT")
            except (TypeError, ValueError):
                pass
            return _real_os_stat(path, *args, **kwargs)

        import os as _os_module
        _real_os_stat = _os_module.stat
        with patch("ihealth.source._BRCTL", _FAKE_COMPRESSION_TOOL), \
             patch("ihealth.source._is_dataless", return_value=True), \
             patch("ihealth.source.subprocess.run", side_effect=self._fake_brctl_ok), \
             patch("ihealth.source.os.stat", side_effect=selective_stat):
            with self.assertRaises(SourceError) as ctx:
                _ensure_materialized(target_path, self.log)
        self.assertIn("materialize 状態確認に失敗", str(ctx.exception))

    def test_polling_timeout_raises(self):
        # 関数頭部判定: dataless / polling 中の os.stat は常に SF_DATALESS のまま
        with patch("ihealth.source._BRCTL", _FAKE_COMPRESSION_TOOL), \
             patch("ihealth.source._is_dataless", return_value=True), \
             patch("ihealth.source.os.stat", side_effect=self._stat_with_flags(_SF_DATALESS)), \
             patch("ihealth.source.subprocess.run", side_effect=self._fake_brctl_ok):
            with self.assertRaises(SourceError) as ctx:
                _ensure_materialized(self.path, self.log)
        self.assertIn("materialize がタイムアウト", str(ctx.exception))


class TestFetchOnceMaterializeIntegration(unittest.TestCase):
    """_fetch_once: dataless 検出 → brctl → decompress の順序が守られる回帰テスト"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.target_date = date(2026, 4, 22)
        self.archive_root = self.root / "archive"
        self._ctool_patcher = patch("ihealth.source.COMPRESSION_TOOL", _FAKE_COMPRESSION_TOOL)
        self._brctl_patcher = patch("ihealth.source._BRCTL", _FAKE_COMPRESSION_TOOL)
        self._poll_patcher = patch("ihealth.source._MATERIALIZE_POLL_INTERVAL_SEC", 0.0)
        self._timeout_patcher = patch("ihealth.source._MATERIALIZE_TIMEOUT_SEC", 0.5)
        self._ctool_patcher.start()
        self._brctl_patcher.start()
        self._poll_patcher.start()
        self._timeout_patcher.start()

    def tearDown(self):
        self._timeout_patcher.stop()
        self._poll_patcher.stop()
        self._brctl_patcher.stop()
        self._ctool_patcher.stop()
        self._tmp.cleanup()

    def test_dataless_files_materialized_before_decompression(self):
        """dataless 状態で開始 → brctl 後に dataless 解除 → decompress 成功"""
        _build_autosync_layout(self.root / "AutoSync", self.target_date)
        import logging
        logger = logging.getLogger("test-materialize-integration")

        # 関数頭部の _is_dataless 判定は常に True (= 全ファイル dataless 扱い).
        # polling 内の os.stat は即座に SF_DATALESS=0 を返して解除完了を演出する.
        # codex round 1 PR #27: Python 3.11+ では pathlib.Path.is_dir() /
        # exists() も os.stat 経由で呼ばれるため、target metric file 以外は
        # 実 os.stat にフォールバックする selective mock にする
        # (3.9 は lstat 経由なので影響なし).
        import os as _os
        _real_stat = _os.stat
        metrics_root = self.root / "AutoSync" / "HealthMetrics"

        def fake_stat(path, *args, **kwargs):
            try:
                p = Path(str(path))
            except (TypeError, ValueError):
                return _real_stat(path, *args, **kwargs)
            # metrics_root 配下の .hae ファイルだけ「st_flags=0 (= 解除完了)」
            # を返す. 親ディレクトリ階層 / archive 出力先 / fake_compression_tool
            # 等は実 stat に委譲.
            try:
                if p.suffix == ".hae" and metrics_root in p.parents:
                    fake = unittest.mock.MagicMock()
                    fake.st_flags = 0
                    # Python 3.12 の C 実装 S_ISREG は int を厳格要求するため、
                    # MagicMock のままだと pathlib.Path.is_file() が TypeError
                    # で落ちる. 通常ファイル相当の mode を明示的に与える.
                    import stat as _stat_mod
                    fake.st_mode = _stat_mod.S_IFREG | 0o644
                    return fake
            except (OSError, ValueError):
                pass
            return _real_stat(path, *args, **kwargs)

        # 呼ばれたコマンド種別を順序保存 (brctl_download → compression_tool が
        # メトリクスごとにペアで並んでいることを検証)
        call_log: list[str] = []

        def fake_run(*args, **kwargs):
            cmd = args[0]
            if cmd[0] == str(_FAKE_COMPRESSION_TOOL) and "download" in cmd:
                call_log.append("brctl_download")
                class _R:
                    returncode = 0
                    stdout = b""
                    stderr = b""
                return _R()
            # compression_tool の呼び出しは既存ヘルパーに委ねる
            call_log.append("compression_tool")
            return _fake_compression_tool_writes_json(*args, **kwargs)

        with patch("ihealth.source._is_dataless", return_value=True), \
             patch("ihealth.source.os.stat", side_effect=fake_stat), \
             patch("ihealth.source.subprocess.run", side_effect=fake_run):
            result = _fetch_once(
                self.root / "AutoSync", self.target_date, self.archive_root, logger,
            )

        self.assertEqual(len(result.archived), len(METRIC_DIRS))
        # 各メトリクスについて brctl_download → compression_tool の順で並んでいる
        for i in range(0, len(call_log), 2):
            self.assertEqual(call_log[i], "brctl_download")
            self.assertEqual(call_log[i + 1], "compression_tool")


class TestFetchMetricForDateMaterialize(unittest.TestCase):
    """fetch_metric_for_date: 単一メトリクス入口でも materialize が走る

    workflow.py:304 (前日 sleep_analysis 併読) はこのエントリを直接叩くため、
    materialize 経路が抜けると本番で前日データが取れず再発する。
    codex review 2026-04-28 で抜けが指摘された経路を回帰固定する。
    """

    def setUp(self):
        from ihealth.source import fetch_metric_for_date
        self._fetch = fetch_metric_for_date
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.target_date = date(2026, 4, 22)
        self.archive_root = self.root / "archive"
        self._ctool_patcher = patch("ihealth.source.COMPRESSION_TOOL", _FAKE_COMPRESSION_TOOL)
        self._brctl_patcher = patch("ihealth.source._BRCTL", _FAKE_COMPRESSION_TOOL)
        self._poll_patcher = patch("ihealth.source._MATERIALIZE_POLL_INTERVAL_SEC", 0.0)
        self._timeout_patcher = patch("ihealth.source._MATERIALIZE_TIMEOUT_SEC", 0.5)
        self._ctool_patcher.start()
        self._brctl_patcher.start()
        self._poll_patcher.start()
        self._timeout_patcher.start()

    def tearDown(self):
        self._timeout_patcher.stop()
        self._poll_patcher.stop()
        self._brctl_patcher.stop()
        self._ctool_patcher.stop()
        self._tmp.cleanup()

    def test_triggers_brctl_then_decompress(self):
        # sleep_analysis のみ配置 (前日併読のシミュレーション)
        _build_autosync_layout(
            self.root / "AutoSync", self.target_date,
            with_metrics={"sleep_analysis"},
        )
        import logging
        import os as _os
        logger = logging.getLogger("test-fmfd-materialize")

        # codex round 1 PR #27: Python 3.11+ pathlib 互換のため selective mock.
        # target metric file (.hae) のみ st_flags=0 を返し、それ以外は real stat.
        _real_stat = _os.stat
        metrics_root = self.root / "AutoSync" / "HealthMetrics"

        def fake_stat(path, *args, **kwargs):
            try:
                p = Path(str(path))
            except (TypeError, ValueError):
                return _real_stat(path, *args, **kwargs)
            try:
                if p.suffix == ".hae" and metrics_root in p.parents:
                    fake = unittest.mock.MagicMock()
                    fake.st_flags = 0
                    # Python 3.12 の C 実装 S_ISREG は int を厳格要求するため、
                    # MagicMock のままだと pathlib.Path.is_file() が TypeError
                    # で落ちる. 通常ファイル相当の mode を明示的に与える.
                    import stat as _stat_mod
                    fake.st_mode = _stat_mod.S_IFREG | 0o644
                    return fake
            except (OSError, ValueError):
                pass
            return _real_stat(path, *args, **kwargs)

        call_log: list[str] = []

        def fake_run(*args, **kwargs):
            cmd = args[0]
            if "download" in cmd:
                call_log.append("brctl_download")
                class _R:
                    returncode = 0
                    stdout = b""
                    stderr = b""
                return _R()
            call_log.append("compression_tool")
            return _fake_compression_tool_writes_json(*args, **kwargs)

        with patch("ihealth.source._is_dataless", return_value=True), \
             patch("ihealth.source.os.stat", side_effect=fake_stat), \
             patch("ihealth.source.subprocess.run", side_effect=fake_run):
            result = self._fetch(
                health_export_dir=self.root / "AutoSync",
                metric_dir_name="sleep_analysis",
                target_date=self.target_date,
                archive_root=self.archive_root,
                logger=logger,
            )

        self.assertIsNotNone(result)
        self.assertEqual(call_log, ["brctl_download", "compression_tool"])

    def test_returns_none_on_materialize_failure(self):
        """brctl 失敗 → SourceError は内部で握って None を返す (graceful)"""
        _build_autosync_layout(
            self.root / "AutoSync", self.target_date,
            with_metrics={"sleep_analysis"},
        )
        import logging
        logger = logging.getLogger("test-fmfd-materialize-fail")

        def _brctl_fail(*args, **kwargs):
            class _R:
                returncode = 1
                stdout = b""
                stderr = b"failed"
            return _R()

        with patch("ihealth.source._is_dataless", return_value=True), \
             patch("ihealth.source.subprocess.run", side_effect=_brctl_fail):
            result = self._fetch(
                health_export_dir=self.root / "AutoSync",
                metric_dir_name="sleep_analysis",
                target_date=self.target_date,
                archive_root=self.archive_root,
                logger=logger,
            )
        self.assertIsNone(result)


class TestCriticalMetricsDefinition(unittest.TestCase):
    """_CRITICAL_METRICS の定義回帰 (Apple 仕様に合わせて heart_rate_resting を除外)"""

    def test_critical_is_step_and_heart_rate_only(self):
        self.assertEqual(_CRITICAL_METRICS, frozenset({"step_count", "heart_rate"}))

    def test_heart_rate_resting_not_in_critical(self):
        # Apple 仕様上、resting heart rate は「正常な欠落」がありうるため除外
        self.assertNotIn("heart_rate_resting", _CRITICAL_METRICS)


if __name__ == "__main__":
    unittest.main()
