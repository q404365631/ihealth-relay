"""Notion API クライアント + DailyHealthData → properties 変換層。

``urllib.request`` + ``json`` のみで Notion API を叩く。外部 PyPI 依存ゼロ。
現時点で提供する機能:

- :meth:`NotionClient.fetch_database_schema`: DB のプロパティ名 → 型 の辞書を取得
- :meth:`NotionClient.find_page_by_date`: 日付フィルタで 1 件だけページ ID を引く
- :meth:`NotionClient.update_page`: ページのプロパティを PATCH で上書き
- :data:`PROPERTY_MAP` / :func:`build_properties`: ``DailyHealthData`` → Notion API
  の ``{"properties": {...}}`` ペイロード変換 (Issue #2)

**本モジュールのスコープ外** (別 Issue で実装):

- 429 レート制限のリトライ (指数バックオフ + Retry-After 尊重): Issue #3
- 新規ページ作成 (create_page): 当面不要 (日記 DB はユーザーが事前に当日ページを作る運用)
- 複数ヒット時の「振り返り未チェック優先」選択: 旧 GoogleFitNotionIntegration 由来の
  ロジック。必要になったら別 Issue で移植

秘密情報の扱い:
- ``Authorization`` ヘッダに ``Bearer {token}`` で渡す
- ログには token を絶対に出さない。ログは ``page_id`` / プロパティ**数**のみ
"""

from __future__ import annotations

import email.utils
import json
import logging
import math
import random
import time
import urllib.error
import urllib.request
from dataclasses import fields as dataclass_fields
from datetime import date, datetime, timezone
from typing import Callable

from ihealth.models import DailyHealthData


#: HTTP 呼び出しのタイムアウト (秒)。launchd 自動実行で「朝起動して夜までハング」を防ぐ。
_HTTP_TIMEOUT_SEC = 30

#: page_id マスク表示時の末尾文字数。UUID 前提なら 8 文字で十分な識別性。
_PAGE_ID_MASK_SUFFIX = 8

#: リトライ関連。Notion API は平均 3 req/sec のレート制限 + 一時的な 5xx も起こる。
#: 最大試行回数 (初回含む) / 初期バックオフ秒 / 最大バックオフ秒 / 最大 Retry-After 秒。
#: 最大バックオフは「launchd 定時実行の 5 分以内に完了する」運用制約から逆算した上限。
_MAX_ATTEMPTS = 3
_BASE_BACKOFF_SEC = 2.0
_MAX_BACKOFF_SEC = 60.0
#: Retry-After の「悪意のサーバから 1 時間後などの巨大値を返された場合」に実運用を守る
#: 安全網。Notion は実質 1〜30 秒しか返さないので超過したら clamp する。
_MAX_RETRY_AFTER_SEC = 60.0

#: モジュール既定のロガー。``build_properties`` がクライアントの外でも使えるように、
#: 明示的にロガーを注入しない場合はこのロガーを使う (運用では ``ihealth`` 配下に
#: 集約されるため ``configure_logger`` の設定が効く)。
_logger = logging.getLogger(__name__)


#: ``DailyHealthData`` のフィールド名 → (Notion プロパティ名, 値変換関数)
#:
#: **既定値は英語 Title Case + 単位 (Phase 1 B2 で日本語から切り替え 2026-04-29)**.
#: 別名の Notion DB (例: 日本語列名) は日本語名 (例 "歩数 (歩)") を
#: 期待するため、互換が必要なユーザーは ``config.toml`` の
#: ``[publishers.notion.fields.<field>].property`` で override する.
#: ``config.example.toml`` に 別名の Notion DB 互換マッピングを記載済み.
#:
#: - **Notion プロパティ名は半角スペース + 半角括弧 + 単位**. Notion 側のプロパティ
#:   名と文字列完全一致させる必要がある.
#: - 値変換関数は ``DailyHealthData`` の raw 値 (hours, km, kg, kcal, bpm, …) を
#:   Notion に書き込む最終値 (分、小数 1 桁丸めなど) に変換する
#: - ``sleep_hours`` / ``nap_hours`` は ``hours`` を持つので 60 倍 → 整数分
#:   (``Sleep Minutes`` / ``Nap Minutes`` というプロパティ名に揃える)
#: - ``mindful_minutes`` は既に分単位なので整数丸めだけ
#: - **丸めは Python 標準の ``round()`` = IEEE 754 half-even (banker's rounding)**
#:   を採用 (例: ``round(65.55, 1) == 65.5``、``round(0.5) == 0``).
#:   SUM 集計の累積誤差で .5 ちょうどはほぼ来ないので実運用への影響は軽微.
#:
#: TODO(Issue #11): 時間系プロパティを分から時間 (小数 2 桁) 表記に移行する予定.
#:   1. Notion DB に ``Sleep Hours`` 等の時間表記カラムを手動追加
#:   2. 既存ページの過去データを時間表記に書き換え (移行スクリプトは Issue #11 に紐付け)
#:   3. 本 dict の sleep_hours/nap_hours/mindful_minutes を時間表記に切り替え.
PROPERTY_MAP: "dict[str, tuple[str, Callable[[object], object]]]" = {
    "step_count":               ("Step Count",                  lambda v: int(v)),
    "distance_km":              ("Distance (km)",               lambda v: round(float(v), 1)),
    "active_energy_kcal":       ("Active Energy (kcal)",        lambda v: round(float(v), 1)),
    "exercise_intensity_score": ("Exercise Intensity Score",    lambda v: round(float(v), 1)),
    "heart_rate_avg":           ("Heart Rate Avg (bpm)",        lambda v: round(float(v), 1)),
    "heart_rate_max":           ("Heart Rate Max (bpm)",        lambda v: round(float(v), 1)),
    "heart_rate_resting":       ("Heart Rate Resting (bpm)",    lambda v: round(float(v), 1)),
    "oxygen_saturation":        ("Oxygen Saturation (%)",       lambda v: round(float(v), 1)),
    "sleep_hours":              ("Sleep Minutes",               lambda v: int(round(float(v) * 60))),
    "nap_hours":                ("Nap Minutes",                 lambda v: int(round(float(v) * 60))),
    "mindful_sessions":         ("Mindful Sessions",            lambda v: int(v)),
    "mindful_minutes":          ("Mindful Minutes",             lambda v: int(round(float(v)))),
    "body_mass_kg":             ("Body Mass (kg)",              lambda v: round(float(v), 1)),
    "body_fat_percentage":      ("Body Fat (%)",                lambda v: round(float(v), 1)),
}


def _validate_property_map() -> None:
    """``PROPERTY_MAP`` のキーが ``DailyHealthData`` のフィールドと 1:1 で一致することを保証する。

    モジュール import 時に 1 回だけ実行する sanity check。
    ``models.py`` 側でフィールドを追加 / rename したときに早期に検知する
    (``build_properties`` の ``getattr(..., None)`` は typo と本当の欠損値を
    同じ ``None`` として飲み込むため、このガードがないと silent drop する)。

    ``date`` フィールドだけは Notion ページの特定に使う別経路なので ``PROPERTY_MAP``
    には入れない (対象外として明示)。
    """
    model_fields = {f.name for f in dataclass_fields(DailyHealthData)} - {"date"}
    map_fields = set(PROPERTY_MAP.keys())
    only_in_model = model_fields - map_fields
    only_in_map = map_fields - model_fields
    if only_in_model or only_in_map:
        raise RuntimeError(
            "notion.PROPERTY_MAP と models.DailyHealthData のフィールドが一致しません: "
            f"only_in_model={sorted(only_in_model)}, only_in_map={sorted(only_in_map)}"
        )


_validate_property_map()


def build_properties(
    health_data: DailyHealthData,
    db_schema: "dict[str, str]",
    *,
    property_overrides: "dict[str, str] | None" = None,
    logger: "logging.Logger | None" = None,
) -> "dict[str, dict]":
    """:class:`DailyHealthData` を Notion API の ``properties`` dict に変換する。

    - ``None`` フィールドは送信しない (Notion 側の既存値を保持する運用)
    - ``db_schema`` に存在しないプロパティは warning ログを残してスキップ
      (DB に ``昼寝時間`` カラム未追加でも他フィールドの書き込みは壊れない)
    - 値変換は :data:`PROPERTY_MAP` の lambda で実施 (単位換算 + 丸め)

    Args:
        health_data: parser が返した 1 日分の集計値
        db_schema: :meth:`NotionClient.fetch_database_schema` の戻り値
        property_overrides: ``config.toml`` 由来の Notion プロパティ名 override
            (``{field_name: notion_property_name}``)。指定があればそのフィールドの
            プロパティ名を上書きする。未指定フィールドは :data:`PROPERTY_MAP` の
            既定値を使う。Phase 1 B1 / Issue #18 で導入。
        logger: 注入ロガー。未指定なら本モジュールのロガーを使う

    Returns:
        Notion API の ``{"properties": {...}}`` 直下の dict。
        例: ``{"歩数 (歩)": {"number": 12345}, "体重 (kg)": {"number": 65.6}}``
    """
    log = logger or _logger
    overrides = property_overrides or {}
    props: "dict[str, dict]" = {}
    for field_name, (default_prop_name, converter) in PROPERTY_MAP.items():
        value = getattr(health_data, field_name, None)
        if value is None:
            continue
        prop_name = overrides.get(field_name, default_prop_name)
        if prop_name not in db_schema:
            # schema と PROPERTY_MAP のズレは起動時に潰せるが、ここでも fail-soft
            # (例えば 別名 DB からの移行途中で「昼寝時間」が未追加でも歩数等は書ける)
            log.warning(
                "Notion DB に %r が無いためスキップ (field=%s)", prop_name, field_name,
            )
            continue
        props[prop_name] = {"number": converter(value)}
    return props


class NotionError(RuntimeError):
    """Notion API 呼び出しで発生する例外。HTTP ステータス + 応答本文を保持する。"""

    def __init__(
        self,
        message: str,
        *,
        status: "int | None" = None,
        response_body: "str | None" = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.response_body = response_body


class NotionClient:
    """Notion API の薄いラッパー。

    インスタンスは ``token`` / ``database_id`` の 2 値のみを保持する。
    呼び出し側 (composition root = ``__main__``) から必要な値だけ注入する設計で、
    ``Config`` オブジェクト全体を渡さない (Issue #16 で提案された過剰結合回避の方針)。
    """

    API_BASE = "https://api.notion.com/v1"
    API_VERSION = "2022-06-28"
    #: 日付プロパティ名の **既定値**. Phase 1 B2 で英語 ``"Date"`` に変更
    #: (別名の Notion DB は ``"日付"``, ``config.toml`` で override 可能).
    DEFAULT_DATE_PROPERTY_NAME = "Date"
    #: 後方互換 alias. 旧 ``NotionClient.DATE_PROPERTY_NAME`` 参照を
    #: 1 release 維持する.
    DATE_PROPERTY_NAME = DEFAULT_DATE_PROPERTY_NAME

    def __init__(
        self,
        token: str,
        database_id: str,
        *,
        logger: "logging.Logger | None" = None,
        date_property_name: "str | None" = None,
    ) -> None:
        self._token = token
        self._database_id = database_id
        self._logger = logger or logging.getLogger(__name__)
        # silent fallback させない. None だけが「override しない (= 既定使用)」
        # を意味する. 空文字 / 空白だけの文字列は parser バグ / config typo の
        # 可能性が高いので fail-fast.
        if date_property_name is None:
            self._date_property_name = self.DEFAULT_DATE_PROPERTY_NAME
        elif not isinstance(date_property_name, str) or not date_property_name.strip():
            raise ValueError(
                "date_property_name は空でない文字列である必要があります "
                f"(got {date_property_name!r})"
            )
        else:
            self._date_property_name = date_property_name.strip()

    @property
    def date_property_name(self) -> str:
        """find_page_by_date が filter キーに使う Notion DB 列名."""
        return self._date_property_name

    # ---------- 公開 API ----------

    def fetch_database_schema(self) -> "dict[str, str]":
        """DB のプロパティ名 → 型 (``number`` / ``formula`` / ``date`` …) の辞書を返す。

        書き込み時に「DB に存在しないプロパティは警告ログでスキップ」判定をするため、
        実行の最初で 1 回だけ叩く想定。
        """
        url = f"{self.API_BASE}/databases/{self._database_id}"
        payload = self._request("GET", url)
        properties = payload.get("properties")
        # `or {}` のような fallback は shape 検証を無効化するので使わない。
        # 異常応答 (keys 欠落 / None / 他型) は NotionError として表に出す。
        if not isinstance(properties, dict):
            raise NotionError(
                f"Notion 応答の properties が dict ではありません: "
                f"{type(properties).__name__}"
            )
        schema: "dict[str, str]" = {}
        for name, prop in properties.items():
            if not isinstance(prop, dict):
                raise NotionError(
                    f"Notion 応答の properties[{name!r}] が dict ではありません: "
                    f"{type(prop).__name__}"
                )
            prop_type = prop.get("type")
            if not isinstance(prop_type, str):
                raise NotionError(
                    f"Notion 応答の properties[{name!r}].type が str ではありません: "
                    f"{type(prop_type).__name__}"
                )
            schema[name] = prop_type
        self._logger.debug("DB schema 取得: %d プロパティ", len(schema))
        return schema

    def find_page_by_date(self, target_date: date) -> "str | None":
        """``target_date`` に一致するページの ID を返す。無ければ ``None``。

        複数ヒット時は ``results[0]`` を採用 (従来運用の「振り返り未チェック
        優先」ロジックは現時点では実装せず、必要になったら別 Issue で移植する)。
        """
        url = f"{self.API_BASE}/databases/{self._database_id}/query"
        # page_size=2 + has_more で「0 / 1 / 複数」判定に必要な最小値。
        body = {
            "filter": {
                "property": self._date_property_name,
                "date": {"equals": target_date.isoformat()},
            },
            "page_size": 2,
        }
        payload = self._request("POST", url, body=body)
        results = payload.get("results")
        if not isinstance(results, list):
            raise NotionError(
                f"Notion 応答の results が list ではありません: "
                f"{type(results).__name__}"
            )
        # `has_more` の shape 検証は results 空判定より**前**に置く
        # (results=[] でも API 契約違反の has_more は拾う)。
        # Notion API 仕様では query endpoint は常に has_more を返すため、
        # key 欠落も shape 違反扱い。
        if "has_more" not in payload:
            raise NotionError(
                "Notion 応答に has_more キーがありません (query endpoint の仕様違反)"
            )
        has_more = payload["has_more"]
        if not isinstance(has_more, bool):
            raise NotionError(
                f"Notion 応答の has_more が bool ではありません: "
                f"{type(has_more).__name__}"
            )
        if not results:
            self._logger.info(
                "Notion 日記ページが見つかりません: date=%s", target_date
            )
            return None
        # 2 件以上 or has_more=True なら「複数」と判定
        has_multiple = len(results) > 1 or has_more
        if has_multiple:
            self._logger.warning(
                "Notion 日記ページが複数ヒット、先頭を採用: date=%s", target_date,
            )
        first = results[0]
        if not isinstance(first, dict):
            raise NotionError(
                f"Notion 応答の results[0] が dict ではありません: {type(first).__name__}"
            )
        page_id = first.get("id")
        if not isinstance(page_id, str) or not page_id:
            raise NotionError(
                f"Notion 応答の results[0].id が文字列ではありません: {type(page_id).__name__}"
            )
        self._logger.debug("Notion page 見つかりました: %s", _mask_page_id(page_id))
        return page_id

    def update_page(
        self, page_id: str, properties: "dict[str, dict]"
    ) -> None:
        """``PATCH /v1/pages/{page_id}`` で properties を差分更新する。

        ``properties`` は Notion API 形式の ``{プロパティ名: {型: 値}, ...}``。
        例: ``{"歩数 (歩)": {"number": 12345}}``。
        値の単位変換・丸め・None スキップは呼び出し側 (Issue #2 の build_properties) の責務。
        """
        if not isinstance(page_id, str) or not page_id:
            raise NotionError(f"page_id が空または文字列ではありません: {page_id!r}")
        if not isinstance(properties, dict):
            raise NotionError(
                f"properties が dict ではありません: {type(properties).__name__}"
            )
        # properties[*] も Notion API 契約 (プロパティ名文字列 → 型別 dict) を満たすこと
        for prop_name, prop_value in properties.items():
            if not isinstance(prop_name, str) or not prop_name:
                raise NotionError(
                    f"properties のキーが空または文字列ではありません: {prop_name!r}"
                )
            if not isinstance(prop_value, dict):
                raise NotionError(
                    f"properties[{prop_name!r}] が dict ではありません: "
                    f"{type(prop_value).__name__}"
                )
        url = f"{self.API_BASE}/pages/{page_id}"
        self._request("PATCH", url, body={"properties": properties})
        self._logger.info(
            "Notion page 更新完了: page_id=%s properties=%d 件",
            _mask_page_id(page_id), len(properties),
        )

    # ---------- 内部ヘルパー ----------

    def _build_headers(self, *, has_body: bool) -> "dict[str, str]":
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Notion-Version": self.API_VERSION,
        }
        if has_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: "dict | None" = None,
    ) -> dict:
        """HTTP 呼び出しの共通処理。4xx/5xx は ``NotionError`` に変換する。

        * **例外メッセージは HTTP status のみ**、応答本文は ``response_body`` 属性に
          保持する (呼び出し側が ``logger.error("%s", exc)`` しても本文を自動ログに
          残さない = 個人情報露出耐性)
        * タイムアウト ``_HTTP_TIMEOUT_SEC`` を全呼び出しに適用 (launchd 運用で
          無限ハング防止)
        * **リトライ (Issue #3)**: 429 / 5xx / URLError は指数バックオフで最大
          ``_MAX_ATTEMPTS`` 回まで再試行する。429 は ``Retry-After`` ヘッダを
          尊重 (秒数 / HTTP-date 両対応)、存在しなければ指数バックオフ。
          4xx (429 を除く) は即座に失敗させる (400 Bad Request / 401 Unauthorized
          /404 Not Found は再送してもエラーが変わらないため)。
        """
        data: "bytes | None" = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            url, data=data, headers=self._build_headers(has_body=data is not None),
            method=method,
        )
        raw = self._urlopen_with_retry(req)

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NotionError(
                f"Notion API 応答の JSON パースに失敗: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise NotionError(
                f"Notion API 応答が dict ではありません: {type(payload).__name__}"
            )
        return payload

    def _urlopen_with_retry(self, req: "urllib.request.Request") -> bytes:
        """``urlopen`` 呼び出しに 429 / 5xx / URLError のリトライを載せる。

        * 429: ``Retry-After`` (秒数 / HTTP-date) を尊重、無ければ指数バックオフ +
          ジッタ。``_MAX_RETRY_AFTER_SEC`` で clamp (異常な巨大値から保護)
        * 500/502/503/504: 指数バックオフ + ジッタ
        * ``URLError`` (接続拒否 / DNS 失敗 / タイムアウト等): 指数バックオフ + ジッタ
        * 4xx 以外 (400/401/403/404 等): 即座に ``NotionError`` raise。
          ``_MAX_ATTEMPTS`` 回試して成功しなかったら最後の例外を ``NotionError``
          にマップして上げる。
        """
        last_error: "Exception | None" = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                last_error = exc
                retry_after = _parse_retry_after(exc.headers.get("Retry-After")) if exc.code == 429 else None
                if _should_retry_http(exc.code) and attempt < _MAX_ATTEMPTS:
                    wait_sec = retry_after if retry_after is not None else _backoff_sec(attempt)
                    self._logger.warning(
                        "Notion API %d, %.1fs 後にリトライ (attempt %d/%d)",
                        exc.code, wait_sec, attempt, _MAX_ATTEMPTS,
                    )
                    time.sleep(wait_sec)
                    continue
                # リトライせず即 raise (4xx など) or 上限到達
                try:
                    err_body = exc.read().decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    err_body = ""
                raise NotionError(
                    f"Notion API エラー HTTP {exc.code}",
                    status=exc.code,
                    response_body=err_body,
                ) from exc
            except urllib.error.URLError as exc:
                # TimeoutError は URLError の subclass 扱い (Python 3.10+) だが、3.9 では
                # socket.timeout が reason に来るケースがある。どちらも URLError で拾える。
                last_error = exc
                if attempt < _MAX_ATTEMPTS:
                    wait_sec = _backoff_sec(attempt)
                    self._logger.warning(
                        "Notion API 接続エラー %s, %.1fs 後にリトライ (attempt %d/%d)",
                        exc.reason, wait_sec, attempt, _MAX_ATTEMPTS,
                    )
                    time.sleep(wait_sec)
                    continue
                raise NotionError(f"Notion API 接続エラー: {exc.reason}") from exc
        # ループが break せず終了 (理論上到達しないが、型チェッカー向けに明示)
        raise NotionError(
            f"Notion API リトライ上限到達 ({_MAX_ATTEMPTS} 回)"
        ) from last_error


def _should_retry_http(status: int) -> bool:
    """HTTP ステータスコードを見てリトライ可否を返す。

    - ``429`` Too Many Requests: リトライ (レート制限)
    - ``500`` / ``502`` / ``503`` / ``504``: リトライ (一時的サーバ障害)
    - それ以外の 4xx (400/401/403/404 など): リトライしない (再送しても同じエラー)
    """
    if status == 429:
        return True
    if status in (500, 502, 503, 504):
        return True
    return False


def _backoff_sec(attempt: int) -> float:
    """指数バックオフ + ジッタ (秒) を返す。

    ``attempt`` は 1-origin。``_BASE_BACKOFF_SEC * 2**(attempt-1)`` をベースに
    ±25% のジッタを乗せる (複数プロセス並列時の thundering herd 回避 + 再現性のため
    決定的な乱数は使わない)。``_MAX_BACKOFF_SEC`` で clamp。
    """
    base = _BASE_BACKOFF_SEC * (2 ** (attempt - 1))
    # ±25% ジッタ
    jitter = random.uniform(-0.25, 0.25) * base
    return min(_MAX_BACKOFF_SEC, max(0.0, base + jitter))


def _parse_retry_after(header_value: "str | None") -> "float | None":
    """``Retry-After`` ヘッダを秒数に変換する (RFC 7231)。

    * 秒数形式 (``"30"``): そのまま float
    * HTTP-date 形式 (``"Wed, 21 Oct 2015 07:28:00 GMT"``): 現在時刻との差 (正のみ)
    * パース失敗 / 負数 / 0 以下 / NaN / ±Infinity: ``None`` を返し、
      呼び出し側が指数バックオフにフォールバック (``time.sleep(nan)`` や
      ``sleep(0)`` 即時バーストを防ぐ)
    * 上限 ``_MAX_RETRY_AFTER_SEC`` を超えたら clamp
      (サーバ側バグで「1 時間待て」等を返されても運用を止めない)
    """
    if header_value is None:
        return None
    stripped = header_value.strip()
    if not stripped:
        return None
    # 秒数形式: 先に数値パースを試す
    try:
        seconds = float(stripped)
    except ValueError:
        seconds = None
    if seconds is not None:
        # NaN / ±Infinity は time.sleep に渡すと ValueError/OverflowError を起こす
        if not math.isfinite(seconds) or seconds <= 0:
            return None
        return min(seconds, _MAX_RETRY_AFTER_SEC)
    # HTTP-date 形式
    try:
        retry_at = email.utils.parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None
    if retry_at is None:
        return None
    # parsedate_to_datetime は tzinfo が無いケースがあるので UTC 扱いに揃える
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    delta = (retry_at - datetime.now(tz=timezone.utc)).total_seconds()
    # 過去日時 (時計ずれ / 壊れたヘッダ) は None にして指数バックオフに委ねる。
    # sleep(0) 即時再送は 429 バースト誘発リスクがあるため避ける。
    if delta <= 0 or not math.isfinite(delta):
        return None
    return min(delta, _MAX_RETRY_AFTER_SEC)


def _mask_page_id(page_id: str) -> str:
    """page_id を末尾 ``_PAGE_ID_MASK_SUFFIX`` 文字だけに省略表示する。

    短い文字列が渡ってきた場合でも一貫して ``...xxxx`` 形式に整形し、
    ログポリシー (内部識別子の全文露出を避ける) を守る。
    """
    if len(page_id) <= _PAGE_ID_MASK_SUFFIX:
        return f"...{page_id}"
    return f"...{page_id[-_PAGE_ID_MASK_SUFFIX:]}"


# ---------- NotionPublisher (Phase 1 A2 で workflow.py から移動) ----------


def _wrap_notion_error(exc: NotionError, target_date: date) -> "NotionAppError":
    """``NotionError`` を ユーザー向け ``NotionAppError`` にラップする.

    workflow.py から移動 (Phase 1 A2). ``NotionAppError`` import は遅延 import で
    ``ihealth.errors`` への循環依存を避ける.
    """
    from ihealth.errors import NotionAppError
    status_tag = f"HTTP {exc.status}" if exc.status is not None else "接続/その他"
    return NotionAppError(
        f"Notion API エラー ({status_tag})",
        title=f"ihealth-relay: Notion API エラー ({status_tag})",
        body=(
            f"対象日 {target_date} の書き込みに失敗しました。\n"
            "logs/run.log を確認してください。"
        ),
    )


class NotionPublisher:
    """本番用 Publisher. Notion API を叩いて対象日のページを更新する.

    Phase 1 A2 で :mod:`ihealth.workflow` から本モジュールに移動.
    ``ihealth.workflow.NotionPublisher`` は本クラスの再エクスポート (後方互換).

    事前条件違反 (日記ページ未作成) / API エラーは :class:`NotionAppError` に
    統一して raise する. 呼び出し側は ``AppError`` を捕まえるだけで終了コードと
    通知メッセージを決定できる.
    """

    def __init__(
        self,
        client: NotionClient,
        target_date: date,
        logger: logging.Logger,
        *,
        property_overrides: "dict[str, str] | None" = None,
    ) -> None:
        self._client = client
        self._target_date = target_date
        self._logger = logger
        self._property_overrides = property_overrides or {}

    def publish(self, health_data: DailyHealthData) -> None:
        from ihealth.errors import NotionAppError
        try:
            schema = self._client.fetch_database_schema()
            page_id = self._client.find_page_by_date(self._target_date)
        except NotionError as exc:
            raise _wrap_notion_error(exc, self._target_date) from exc

        if page_id is None:
            self._logger.error(
                "Notion 日記ページが存在しません (日付=%s)", self._target_date,
            )
            raise NotionAppError(
                f"日記ページが見つかりません ({self._target_date})",
                title=f"ihealth-relay: 日記ページが見つからない ({self._target_date})",
                body="Notion 日記 DB に対象日のページを作成してから再実行してください。",
            )

        props = build_properties(
            health_data, schema,
            property_overrides=self._property_overrides,
            logger=self._logger,
        )
        # props が空のとき (= DB 列名と PROPERTY_MAP / config.toml が一致しない)
        # silent な exit 0 にせず fail-fast. 日本語名の DB を使い
        # 続けるユーザーが ``config.toml`` を置き忘れたまま B2 で英語 default
        # に切り替わると "更新 0 件・exit 0" で気付けないため.
        populated_field_count = sum(
            1 for key, value in health_data.as_dict().items()
            if key != "date" and value is not None
        )
        if populated_field_count > 0 and not props:
            raise NotionAppError(
                "Notion DB の列名が現在の設定と一致しません",
                title=f"ihealth-relay: Notion DB 列名未移行 ({self._target_date})",
                body=(
                    f"集計値が {populated_field_count} 件あるのに Notion DB の "
                    "どのプロパティ名にも一致しませんでした.\n"
                    "旧日本語 DB を使う場合は config.example.toml を config.toml "
                    "として配置し、[publishers.notion] date_property = '日付' を "
                    "含めて再実行してください."
                ),
            )
        if not props:
            self._logger.warning(
                "送信すべきプロパティが 1 件もありません (populated=0)",
            )
            return

        try:
            self._client.update_page(page_id, props)
        except NotionError as exc:
            raise _wrap_notion_error(exc, self._target_date) from exc

        self._logger.info(
            "Notion 更新成功: target=%s 送信=%d 件", self._target_date, len(props),
        )
