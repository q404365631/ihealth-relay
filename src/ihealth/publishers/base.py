"""Publisher Protocol の単一定義 (Phase 1 A2 で集約).

旧来は :mod:`ihealth.workflow` 内に定義していたが、新たな publisher を
追加するときの import 順 / 循環依存リスクを避けるため、本モジュールに
切り出した. ``ihealth.workflow.Publisher`` は本モジュールの再エクスポート.

Protocol だけを置き、具象 publisher (``NotionPublisher`` /
``DryRunPublisher`` / ``MarkdownPublisher`` ...) は各 publisher モジュール
側に置く.
"""

from __future__ import annotations

from typing import Protocol

from ihealth.models import DailyHealthData


class Publisher(Protocol):
    """``DailyHealthData`` の出力先を差し替え可能にするプロトコル.

    - 本番 Notion: :class:`ihealth.publishers.notion.NotionPublisher`
    - ``--dry-run``: :class:`ihealth.publishers.dryrun.DryRunPublisher`
    - Markdown: :class:`ihealth.publishers.markdown.MarkdownPublisher`
    - stdout: :class:`ihealth.publishers.stdout.StdoutPublisher`
    - SQLite: :class:`ihealth.publishers.sqlite.SQLitePublisher`
    - Slack: :class:`ihealth.publishers.slack.SlackPublisher`

    TODO(拡張性): 現在は ``publish(health_data)`` の 1 引数だが、Slack/CSV
    向けには ``target_date`` / ``archive_dir`` / ``populated`` 等のコンテキスト
    が必要になる場合がある. 現時点では各 publisher が constructor で必要な
    context を受け取る設計で対応している. 将来 publisher を追加するときに
    ``PublishContext`` dataclass を導入して ``publish(context)`` に進化
    させることを検討する.
    """

    def publish(self, health_data: DailyHealthData) -> None: ...
