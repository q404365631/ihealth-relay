"""`logging` モジュールの設定。

ログ出力先:
- 標準エラー (launchd や手動実行の端末に流れる)
- ``<project_root>/logs/run.log`` (日次ローテーション、30 日保持)

将来の拡張 (ヘルスデータの可視化・サマリ自動生成など) を見据え、
入力 (``data/health/``) と合わせて 1 リポジトリ配下に蓄積する方針。
``logs/`` は ``.gitignore`` 対象。

日本語メッセージをそのまま書き出すため UTF-8 を明示する。
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import IO

# src/ihealth/logger.py から 3 階層上 = プロジェクトルート
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_LOG_DIR = _PROJECT_ROOT / "logs"
_LOG_FILE = _LOG_DIR / "run.log"
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 許容ログレベル (大文字小文字不問)。config.py もこれを参照する。
ALLOWED_LEVELS: "frozenset[str]" = frozenset({"debug", "info", "warning", "error"})


def configure(
    level: str = "info",
    *,
    log_file: "Path | None" = None,
    stream: "IO[str] | None" = None,
    enable_file_handler: bool = True,
) -> logging.Logger:
    """``ihealth`` 名前空間のロガーに handler を設定して返す。

    ルートロガーには触らない (テスト・他のライブラリの handler を破壊しない)。
    ``logger.propagate = False`` で ``ihealth`` 配下のログが root に伝播しないようにし、
    既存 handler は明示的に ``close()`` してから外すことで FD リークを防ぐ。

    **Issue #17 で引数化**: テストで実ファイル ``logs/run.log`` を開かずに
    stream だけ差し替えたいケースや、launchd 実行時に別パスにログを出したい
    ケースに対応するため、出力先をすべて依存性注入可能にした。

    Args:
        level: ``debug`` / ``info`` / ``warning`` / ``error`` のいずれか
            (大文字小文字不問)。不正値は ``ValueError``。
        log_file: ファイルログの出力先。``None`` なら ``<project_root>/logs/run.log``。
        stream: ``StreamHandler`` の出力先 IO。``None`` なら sys.stderr。
        enable_file_handler: ``False`` で ``TimedRotatingFileHandler`` を付けない
            (ユニットテストで実ファイル FD を作らない用途)。

    Returns:
        設定済みの ``logging.Logger``。

    Raises:
        ValueError: ``level`` が ``ALLOWED_LEVELS`` に含まれない場合。
    """
    normalized = level.strip().lower()
    if normalized not in ALLOWED_LEVELS:
        raise ValueError(
            f"LOG_LEVEL が不正です (許可値: {sorted(ALLOWED_LEVELS)}): {level!r}"
        )
    log_level = getattr(logging, normalized.upper())
    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    handlers: "list[logging.Handler]" = []
    if enable_file_handler:
        target_file = log_file if log_file is not None else _LOG_FILE
        target_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            target_file,
            when="midnight",
            backupCount=30,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    stream_handler = logging.StreamHandler(stream)  # stream=None → sys.stderr
    stream_handler.setFormatter(formatter)
    handlers.append(stream_handler)

    logger = logging.getLogger("ihealth")
    # 既存 handler があれば close してから外す (同一プロセス内で configure が
    # 複数回呼ばれるテストや将来の再初期化で FD がリークしないように)
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        try:
            existing.close()
        except Exception:  # noqa: BLE001 (close のベストエフォートで十分)
            pass
    for h in handlers:
        logger.addHandler(h)
    logger.setLevel(log_level)
    logger.propagate = False  # root logger への多重出力を防ぐ

    return logger
