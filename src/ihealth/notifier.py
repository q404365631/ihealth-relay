"""Reminders.app 連携 (AppleScript 経由の永続通知)。

launchd で毎朝自動実行される前提なので、失敗に **永続的** に気付ける通知経路が必要。
通知センターは自動消滅するため不適。Reminders.app は:

- ユーザーが完了するまでリマインダーが残る
- iCloud 同期で iPhone / Apple Watch にも即座に届く
- macOS 標準搭載、追加インストール不要
- AppleScript で外部から作成可能

設計ポリシー:

- **通知失敗で本体処理の終了コードが変わらない**: 通知はあくまで「副作用」で、
  本体の success/fail 判定を通知手段の有無に依存させない。
  例外 (osascript 不在 / AppleScript エラー / タイムアウト) はすべて握り潰して
  error ログだけ残す。
- **AppleScript インジェクション対策**: リマインダー本文はユーザー由来の値
  (Notion エラー本文、ファイルパス等) を含むので、バックスラッシュ /
  ダブルクオート / 改行を正しくエスケープする。
- **絶対パス呼び出し**: ``launchd`` 配下の実行環境は ``PATH`` が貧弱なので、
  ``osascript`` を ``/usr/bin/osascript`` で直接呼ぶ。
"""

from __future__ import annotations

import logging
import os
import subprocess


_logger = logging.getLogger(__name__)

#: ``osascript`` の絶対パス。macOS では常に ``/usr/bin/osascript``。
#: launchd 経由だと ``PATH`` が未定義のことがあるため ``which`` でなく固定。
_OSASCRIPT = "/usr/bin/osascript"

#: ``osascript`` の実行タイムアウト (秒)。通常 100ms 程度で返るので余裕を持って 10 秒。
_OSASCRIPT_TIMEOUT_SEC = 10


def _escape_applescript_string(s: str) -> str:
    """AppleScript 文字列リテラル向けのエスケープ。

    - ``\\`` は先にエスケープ (他のエスケープ文字を二重置換しないため)
    - ``"`` と改行 (``\\n`` / ``\\r``) を AppleScript エスケープシーケンスに変換
    - 他の制御文字はそのまま通す (AppleScript が扱えなければ osascript が失敗する)
    """
    return (
        s.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\n", "\\n")
         .replace("\r", "\\r")
    )


def _build_applescript(title: str, body: str) -> str:
    """Reminders.app にリマインダーを作成する AppleScript を生成する。"""
    return (
        'tell application "Reminders"\n'
        '    make new reminder with properties '
        f'{{name:"{_escape_applescript_string(title)}", '
        f'body:"{_escape_applescript_string(body)}"}}\n'
        'end tell\n'
    )


def notify_failure(title: str, body: str) -> None:
    """Reminders.app にリマインダーを追加する。

    失敗しても例外を投げない (通知手段の失敗で本体処理の終了コードが変わらない設計)。
    Linux / SSH セッション / osascript 非存在環境では warning ログを残してスキップ。
    """
    if not os.path.exists(_OSASCRIPT):
        _logger.warning("osascript が見つからないため通知スキップ: %s", title)
        return

    script = _build_applescript(title, body)
    try:
        subprocess.run(
            [_OSASCRIPT, "-e", script],
            check=True,
            timeout=_OSASCRIPT_TIMEOUT_SEC,
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        _logger.error("Reminders 通知がタイムアウト: %s", title)
    except subprocess.CalledProcessError as exc:
        # stderr にエラー詳細が入る (通常は小さい)。個人情報のリスクは低いが DEBUG に留める
        _logger.error("Reminders 通知失敗 (rc=%d): %s", exc.returncode, title)
        if exc.stderr:
            try:
                stderr_text = exc.stderr.decode("utf-8", errors="replace")
            except (AttributeError, UnicodeDecodeError):
                stderr_text = repr(exc.stderr)
            _logger.debug("osascript stderr: %s", stderr_text)
    except OSError as exc:
        # osascript のファイル自体はあるが実行不可などのケース
        _logger.error("Reminders 通知で OS エラー: %s", exc)
