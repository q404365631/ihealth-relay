"""テスト共通セットアップ。

テスト実行時のログノイズを抑える (`source.py` や `parser.py` の warning / error は
実装の挙動確認であって、ユニットテストの期待挙動そのものはログに依存しない)。
ログ挙動そのものをテストする場合は各テストで ``assertLogs`` を使う。
"""

from __future__ import annotations

import logging

# INFO 以下を全ての logger で黙らせる (WARNING / ERROR / CRITICAL は通す)。
# WARNING を残す理由: notion.build_properties の「DB 未存在プロパティ」など、
# 仕様の一部として assertLogs で検証する warning があるため。INFO / DEBUG は
# テストノイズの大半を占める retry ログや成功時のアーカイブ報告なので抑制で OK。
logging.disable(logging.INFO)
