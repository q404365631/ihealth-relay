"""Slack Publisher: Incoming Webhook で日次サマリを Slack に投稿する Publisher.

Phase 1 A3-slack で導入.

設計方針:
- **Incoming Webhook URL に POST** (``services.slack.com`` への HTTPS POST).
  Slack 認証は URL に含まれるトークンに依存し、別途 API token / OAuth は不要.
- **外部依存ゼロ**: stdlib ``urllib.request`` + ``json`` のみ.
- **メッセージは Markdown 風テキスト**: Slack の ``mrkdwn`` でレンダリング.
  ``*太字*`` / ``• リスト`` / ``\\n`` 改行を使った人間可読サマリを送る.
  Block Kit は v0.2.0 で検討 (今は text フィールドだけ).
- **HTTP タイムアウト 15 秒**: launchd one-shot 前提なので長く待たせない.
  Slack 側がレスポンス遅延すれば fail-fast する方が運用上わかりやすい.
- **リトライしない**: Notion publisher は 429 / 5xx で再試行するが、Slack の
  Incoming Webhook は失敗してもユーザーへの主目的 (サマリ通知) が遅延するだけ
  なので、シンプルに 1 回失敗したら諦めて終了コードに乗せる. リトライが欲しく
  なれば別途 Issue で追加.
- **本文に値を埋め込む**: ``date`` + ``step_count`` 等を ``DailyHealthData`` から
  生成する. ``None`` フィールドは「測定なし」と表記してスキップ.
- **Webhook URL は秘密情報**: ログには絶対に出さない. host 部分のみ表示する.

I/O / 値検証失敗は :class:`SlackPublishError` に統一して raise.
workflow 層が :class:`SlackAppError` (``exit_code=10``) にラップする.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from ihealth.models import DailyHealthData
from ihealth.publishers._payload import (
    PayloadValidationError,
    build_payload,
)


#: HTTP timeout (seconds) for the webhook POST.
_HTTP_TIMEOUT_SEC = 15

#: Maximum length of the URL host segment to include in error messages.
#: Webhook URL の secret token を露出しないため、host 部分のみ表示する.
_URL_HOST_MAX_LEN = 48

#: 許可する Slack Webhook の host.
#: Slack 公式の Incoming Webhook は ``hooks.slack.com``. GovSlack は
#: ``hooks.slack-gov.com``. それ以外への送信は health summary の外送リスク
#: なので拒否する.
ALLOWED_WEBHOOK_HOSTS: "frozenset[str]" = frozenset({
    "hooks.slack.com",
    "hooks.slack-gov.com",
})


class SlackPublishError(RuntimeError):
    """Slack publisher の I/O / 値検証で発生する例外."""


def _mask_webhook_url(url: str) -> str:
    """``https://hooks.slack.com/services/T0XXXX/B0XXXX/secret`` を host だけに省略.

    secret token を含む URL をログに出さないための防御. ``parsed.hostname``
    を使うことで ``https://secret@host/...`` のような userinfo 形式でも
    secret 部分を露出しない.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or "(unknown host)"
    except Exception:  # noqa: BLE001 — defensive fallback
        return "(invalid url)"
    if len(host) > _URL_HOST_MAX_LEN:
        return host[:_URL_HOST_MAX_LEN] + "..."
    return host


def _redact_secret(text: object, webhook_url: str, masked_host: str) -> str:
    """``text`` (例: 例外メッセージ) に含まれる secret URL を host のみに置換.

    単純な ``str.replace(webhook_url)``
    だと case 違い / URL-encoded / path-only 部分文字列で漏洩していた. すべての
    変種を生成して長い順に置換することで、典型的な経路 (URLError.reason,
    HTTPError body, "200+body!=ok" body) で secret が user_message に乗らない
    ようにする.

    Replacement candidates:

    - ``webhook_url`` 原文
    - ``webhook_url.lower()`` (case-insensitive matching の代用)
    - ``parsed.path`` (= ``/services/T0/B0/SECRET``) — secret 単体での出力
    - ``urllib.parse.quote(webhook_url, safe="")`` — URL-encoded form
    - ``urllib.parse.quote(parsed.path, safe="/")`` — path だけ encoded
    - **secret token 単独** (= path の最後のセグメント) もカバー

    長い候補から順に置換することで、部分一致による先勝ちを避ける.
    """
    raw = str(text)
    if not webhook_url:
        return raw

    parsed = urllib.parse.urlparse(webhook_url)
    replacement = f"https://{masked_host}/..."

    # URL / path 全体を case-insensitive で置換
    urlish_needles = [
        webhook_url,
        urllib.parse.quote(webhook_url, safe=""),
    ]
    if parsed.path:
        urlish_needles.append(parsed.path)
        urlish_needles.append(urllib.parse.quote(parsed.path, safe="/"))
    for needle in sorted({n for n in urlish_needles if n}, key=len, reverse=True):
        raw = re.sub(re.escape(needle), replacement, raw, flags=re.IGNORECASE)

    # secret token 単独 (例: ``SECRET`` 部分の path tail). webhook URL の
    # path 単体置換では tail 部分だけが裸で出てくるケースを拾えないため
    # 別経路で boundary 付き置換する. 短い token (< 8 文字) は誤検知が大きい
    # ので個別ケースにのみ反応させる.
    # boundary に ``/`` を **含めない**: full path
    # は先に潰してあるので tail-only の境界として ``/`` は word boundary
    # 扱いの方が ``/SECRET_TOKEN`` / ``SECRET_TOKEN/`` 形式を取りこぼさない.
    if parsed.path:
        path_parts = parsed.path.strip("/").split("/")
        if path_parts:
            secret_tail = path_parts[-1]
            if len(secret_tail) >= 8:
                raw = re.sub(
                    rf"(?<![A-Za-z0-9._~+\-]){re.escape(secret_tail)}(?![A-Za-z0-9._~+\-])",
                    replacement,
                    raw,
                    flags=re.IGNORECASE,
                )
    return raw


def validate_webhook_url(raw: str, *, source_label: str) -> str:
    """Slack Incoming Webhook URL の構造を検証して正規化する.

    config / CLI 側双方で使うため共通実装. 検証内容:

    - scheme は ``https`` のみ (HTTP / file:// / その他は reject)
    - host は :data:`ALLOWED_WEBHOOK_HOSTS` のいずれか
      (= 任意 HTTPS サーバへの health summary 外送を防ぐ)
    - userinfo (``https://user:pass@host/...``) は禁止. **空 userinfo**
      (= ``https://@host/...``) も netloc 内の ``@`` で検知.
    - port は 443 (default = None) のみ. 別 port を指す URL は spoof 疑い.
    - path は ``/services/{T}/{B}/{token}`` の **正確に 4 セグメント** で
      ``..``/``.``/空セグメント / URL-encoded char を含まない.
    - query / fragment は禁止 (誤コピペ検知).

    Args:
        raw: 検証対象の URL 文字列.
        source_label: エラーメッセージ用のソース表示名 (例:
            ``"SLACK_WEBHOOK_URL"`` / ``"--slack-webhook"``).

    Raises:
        ValueError: いずれかの検証に失敗した場合. メッセージに secret token は
            含めない (= source_label と「不正の理由」だけを返す).
    """
    if not raw:
        raise ValueError(f"{source_label} は空です")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme.lower() != "https":
        raise ValueError(
            f"{source_label} は https:// で始まる URL である必要があります"
        )
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_WEBHOOK_HOSTS:
        allowed = ", ".join(sorted(ALLOWED_WEBHOOK_HOSTS))
        raise ValueError(
            f"{source_label} の host が許可リスト外: 許可={allowed}"
        )
    # userinfo 検知: parsed.username/password だけでは "https://@host/..." の
    # 空 userinfo を取りこぼす. netloc 内の "@" 直接検知する (IPv6 の ``[..]``
    # 内には @ が含まれないので bracket 終端後を見れば false positive はない).
    netloc_after_bracket = parsed.netloc.rsplit("]", 1)[-1]
    if "@" in netloc_after_bracket:
        raise ValueError(
            f"{source_label} に user:password 形式は指定できません"
        )
    # port: None (default 443) 以外は拒否. Slack 公式は 443 のみ.
    # ``parsed.port`` は ``:abc`` / ``:99999`` で stdlib ValueError を投げる
    # ため try で受けて source_label を含むメッセージに変換.
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(f"{source_label} の port が不正です") from None
    if port is not None and port != 443:
        raise ValueError(
            f"{source_label} は port 指定不可 (default 443 のみ): 指定 port={port}"
        )
    # path: 厳密に 3 セグメント (``/services/{T}/{B}/{token}`` = ``""``,
    # ``services``, T, B, token の split で len=5)
    parts = parsed.path.split("/")
    if (
        len(parts) != 5
        or parts[0] != ""
        or parts[1] != "services"
        or any(p in ("", ".", "..") for p in parts[2:])
    ):
        raise ValueError(
            f"{source_label} の path は /services/{{T}}/{{B}}/{{token}} 形式である必要があります"
        )
    # URL-encoded path 拒否: ``%2e%2e`` のような escape を unquote した結果が
    # 元と違ったら decoded 経路を疑う.
    if urllib.parse.unquote(parsed.path) != parsed.path:
        raise ValueError(
            f"{source_label} の path に URL-encoded 文字を含むことはできません"
        )
    # path セグメントに ASCII 以外 / 空白 / 制御文字が混入していないか
    #: literal Unicode token (例 ＳＥＣＲＥＴ)
    # や空白を含む token は Slack の正規 token ではない.
    for seg in parts[2:]:
        if not seg.isascii():
            raise ValueError(
                f"{source_label} の path に non-ASCII 文字を含むことはできません"
            )
        if any(ch.isspace() or not ch.isprintable() for ch in seg):
            raise ValueError(
                f"{source_label} の path に空白 / 制御文字を含むことはできません"
            )
    if parsed.query or parsed.fragment:
        raise ValueError(
            f"{source_label} に query / fragment は指定できません"
        )
    return raw


def render_message(health_data: DailyHealthData) -> str:
    """``DailyHealthData`` を Slack ``mrkdwn`` 互換のサマリ文字列に変換する.

    例::

        *2026-04-22 健康データ*
        • 歩数: 12345 歩
        • 移動距離: 5.23 km
        • 睡眠時間: 7.5 h

    - 1 行目は太字の見出し (``*...*``).
    - 各フィールドは bullet list. 単位は人間可読な表記 (歩 / km / kg / kcal /
      bpm / % / h / 分) を付ける.
    - ``None`` フィールドは省略 (Notion publisher と同じセマンティクス).
    """
    try:
        payload = build_payload(health_data)
    except PayloadValidationError as exc:
        raise SlackPublishError(str(exc)) from exc

    iso = payload["date"]
    lines = [f"*{iso} 健康データ*"]
    for field_name, label, unit in _FIELD_LABELS:
        value = payload.get(field_name)
        if value is None:
            continue
        lines.append(f"• {label}: {value} {unit}".rstrip())
    if len(lines) == 1:
        # 全フィールド None: 計測なしの日 (週末・センサー断・iPhone ロック等)
        lines.append("• 計測データなし")
    return "\n".join(lines)


#: 日本語ラベル + 単位. dataview 等の機械処理では使わない (= Slack 表示専用).
_FIELD_LABELS: "list[tuple[str, str, str]]" = [
    ("step_count",               "歩数",         "歩"),
    ("distance_km",              "移動距離",     "km"),
    ("active_energy_kcal",       "消費カロリー", "kcal"),
    ("exercise_intensity_score", "運動強度",     "pt"),
    ("heart_rate_avg",           "平均心拍",     "bpm"),
    ("heart_rate_max",           "最大心拍",     "bpm"),
    ("heart_rate_resting",       "安静時心拍",   "bpm"),
    ("oxygen_saturation",        "酸素飽和度",   "%"),
    ("sleep_hours",              "睡眠時間",     "h"),
    ("nap_hours",                "昼寝時間",     "h"),
    ("mindful_sessions",         "瞑想回数",     "回"),
    ("mindful_minutes",          "瞑想時間",     "分"),
    ("body_mass_kg",             "体重",         "kg"),
    ("body_fat_percentage",      "体脂肪率",     "%"),
]


class SlackPublisher:
    """``DailyHealthData`` を Slack Incoming Webhook で投稿する.

    Args:
        webhook_url: Incoming Webhook URL (``https://hooks.slack.com/services/...``).
            secret token を含むため、ログに出さない.
        target_date: 対象日. ``health_data.date`` との不一致を弾く.
        logger: 注入ロガー.

    Raises (publish 内):
        SlackPublishError: target_date 不整合 / 値検証失敗 / HTTP 失敗.
    """

    def __init__(
        self,
        webhook_url: str,
        target_date: date,
        logger: logging.Logger,
    ) -> None:
        if not webhook_url or not isinstance(webhook_url, str):
            raise ValueError("webhook_url は空でない文字列である必要があります")
        self._webhook_url = webhook_url
        self._target_date = target_date
        self._logger = logger

    def publish(self, health_data: DailyHealthData) -> None:
        if health_data.date != self._target_date:
            raise SlackPublishError(
                f"target_date 不整合: publisher={self._target_date} "
                f"data={health_data.date}"
            )

        # render_message 内で SlackPublishError を投げる経路があるので、
        # ここでは catch しない (= 上位に伝播).
        text = render_message(health_data)

        body = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._webhook_url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        masked_host = _mask_webhook_url(self._webhook_url)
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as resp:
                resp_body = resp.read().decode("utf-8", errors="replace")
                if resp.status != 200:
                    body_redacted = _redact_secret(
                        resp_body, self._webhook_url, masked_host,
                    )
                    raise SlackPublishError(
                        f"Slack Webhook が HTTP {resp.status}: "
                        f"host={masked_host} body={body_redacted!r}"
                    )
                if resp_body.strip() != "ok":
                    # Slack の Incoming Webhook は成功時 "ok" を返す.
                    # 200 でも本文が "ok" 以外なら configuration 異常を疑う
                    # (例: app uninstalled で "no_team" が返る等).
                    # body に secret が含まれる可能性は低いが防御的に redact.
                    body_redacted = _redact_secret(
                        resp_body, self._webhook_url, masked_host,
                    )
                    raise SlackPublishError(
                        f"Slack Webhook 応答が想定外 (200 だが 'ok' でない): "
                        f"host={masked_host} body={body_redacted!r}"
                    )
        except urllib.error.HTTPError as exc:
            # - exc.url / exc.filename には full URL (secret 含む) が残る.
            #   ``raise ... from exc`` だと __cause__ から secret が露出.
            #   ``raise ... from None`` で chain 切断する.
            # - exc.read() で読んだ後 exc.close() しないと fp が GC まで残る.
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                err_body = ""
            finally:
                try:
                    exc.close()
                except Exception:  # noqa: BLE001
                    pass
            err_body_redacted = _redact_secret(
                err_body, self._webhook_url, masked_host,
            )
            raise SlackPublishError(
                f"Slack Webhook が HTTP {exc.code}: host={masked_host} "
                f"body={err_body_redacted!r}"
            ) from None
        except urllib.error.URLError as exc:
            # 接続拒否 / DNS 失敗 / タイムアウト等. ``exc.reason`` に full URL
            # が含まれることがある (例: socket.gaierror が full URL を持つ).
            # secret を含む文字列は redact してから user_message に埋める.
            # __cause__ も secret を持つので ``from None`` で chain 切断.
            reason_redacted = _redact_secret(
                exc.reason, self._webhook_url, masked_host,
            )
            raise SlackPublishError(
                f"Slack Webhook 接続エラー: host={masked_host} reason={reason_redacted}"
            ) from None

        self._logger.info(
            "Slack 通知成功: target=%s host=%s text_len=%d",
            self._target_date, masked_host, len(text),
        )
