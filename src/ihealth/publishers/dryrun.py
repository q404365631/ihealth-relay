"""``--dry-run`` 用 Publisher (Phase 1 A2 で集約).

旧来は :mod:`ihealth.workflow` に定義されていた. publisher を一箇所に
集めるため本モジュールに移動. ``ihealth.workflow.DryRunPublisher`` は
本モジュールの再エクスポートで後方互換維持.

HTTP を 1 回も発射しない publisher で、Notion publisher の組み立て結果
をログに出して終わる. ``--dry-run`` のセマンティクス (= 副作用なしの
動作確認) を実現する.
"""

from __future__ import annotations

import logging

from ihealth.models import DailyHealthData
from ihealth.publishers.notion import PROPERTY_MAP, build_properties


class DryRunPublisher:
    """``--dry-run`` 用. HTTP を 1 回も発射せず組み立てた properties をログに出す.

    Args:
        logger: 注入ロガー.
        property_overrides: ``config.toml`` 由来の Notion プロパティ名 override.
            指定があれば dry-run でも override 後の名前で組み立てる.
    """

    def __init__(
        self,
        logger: logging.Logger,
        *,
        property_overrides: "dict[str, str] | None" = None,
    ) -> None:
        self._logger = logger
        self._property_overrides = property_overrides or {}

    def publish(self, health_data: DailyHealthData) -> None:
        # DB schema を引かない代わりに PROPERTY_MAP 全体が DB に存在する想定で組む.
        # config.toml の property override が指定されていれば、override 後の
        # プロパティ名で dry schema を組み直す (= dry-run でも override 効果を可視化).
        # 実送信時は schema 差分を fetch_database_schema + build_properties の
        # fail-soft スキップで解決する (これは dry-run ではなく本番経路の責務).
        dry_schema: "dict[str, str]" = {}
        for field_name, (default_prop, _converter) in PROPERTY_MAP.items():
            prop_name = self._property_overrides.get(field_name, default_prop)
            dry_schema[prop_name] = "number"
        props = build_properties(
            health_data, dry_schema,
            property_overrides=self._property_overrides,
            logger=self._logger,
        )
        self._logger.info("--dry-run: Notion に送らず properties を組み立てるのみ")
        self._logger.info("組み立てた properties (%d 件):", len(props))
        for name, payload in props.items():
            self._logger.info("  %s = %s", name, payload)
