"""`data/health/YYYY-MM-DD/*.json` を間引いて `tests/fixtures/` に保存する。

テスト用 fixture を生成するためのスクリプト。本プロジェクトは **外部依存ゼロ**
ポリシーなので Python 標準ライブラリだけで実装する。

背景:

* `active_energy.json` 1 日分は 14 MB / 60 000+ レコードあり、git に直接は入れられない
* テストは「型と None/not-None の挙動」の回帰を見たいだけで、正確な累積値は不要
* data 配列を ``_sample`` で間引き、他のキー (``metric`` / ``date`` / ``source``) は
  そのまま保持することで、parser の単位判定・二重計上排除ロジックを検証できるようにする

使い方::

    /usr/bin/python3 scripts/make_fixtures.py 2026-04-22 2026-04-23
    /usr/bin/python3 scripts/make_fixtures.py 2026-04-22 --limit 50 --mode head
    /usr/bin/python3 scripts/make_fixtures.py 2026-04-22 --no-anonymize  # 実機データ含む生 fixture (debug 用、コミット禁止)

間引きモード:

* ``evenly`` (既定): 等間隔 N レコード。各区間の中央点を拾うので head / tail bias なし。
  SUM 系集計 (step_count / active_energy_kcal) で時系列の偏りを抑えたい場合に推奨。
  本リポジトリの fixture はこのモードで生成している。
* ``head``: 先頭 N レコード。実装が最小で早朝のサンプルだけ取れる (時系列偏りあり)
* ``tail``: 末尾 N レコード。睡眠の end-of-day 判定など末尾重視のケース用

**Anonymization (既定で有効)**:

OSS リポジトリにコミットする fixture は、device UUID や使用アプリ identifier
(`com.apple.health.<UUID>` / `jp.co.tanita-thl.HealthPlanet` 等) を残すと
device fingerprint や個人特定可能情報の漏洩につながる。`--anonymize` (既定 ON)
は ``data[*].sources[*].identifier`` と ``name`` を以下にマッピング置換し、
``com.apple.health.*`` 形式の UUID 部分は ``00000000-0000-0000-0000-000000000000``
に固定する::

    Apple Watch.*       → "Sample Watch"
    HealthPlanet (タニタ) → "Sample Scale"          / com.example.scale
    AutoSleep            → "Sample Sleep Tracker"  / com.example.sleeptracker
    Muse                 → "Sample Mindfulness Device" / com.example.mindfulness
    その他               → "Sample <Type>" / com.example.unknown

未知の identifier が来ると警告を出して `unknown` にマップする。新しい
ソースアプリが現れたら ``_ANONYMIZE_SOURCES`` を更新するか、
``--no-anonymize`` で生 fixture を作って中身を確認する。

数値と timestamp は parser 集計ルールの回帰検証に必要なため anonymize 対象外。
公開リポジトリにコミットされる fixture は **必ず anonymized なまま** であること
(``git diff`` で確認する).

終了コード: 全日付が成功したら 0、1 日でも src_dir が無い/壊れていれば 1。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List

#: 既定の間引き上限。100 レコードあれば SUM 系でも代表性が取れ、
#: かつ全 11 ファイル合計でも数十 KB 以内に収まる。
_SAMPLE_MAX = 100

#: 出力 JSON のインデント。tests/ の diff レビュー容易化のため改行あり。
_INDENT = 2

#: HealthKit デバイス UUID (com.apple.health.<UUID>) の placeholder.
_ANON_HEALTHKIT_UUID = "00000000-0000-0000-0000-000000000000"

#: 既知 source の anonymization map: (substring patterns → (display name, identifier)).
#: identifier はエコシステム想定の reverse-DNS 形式. 既知でない identifier は
#: ``("Sample Unknown Device", "com.example.unknown")`` にマップして警告.
#: 新しいソースアプリが出現したら本 dict を更新する.
_ANONYMIZE_SOURCES: "list[tuple[tuple[str, ...], tuple[str, str]]]" = [
    (("apple watch", "iphone"),         ("Sample Watch", "com.apple.health.SAMPLE")),
    (("healthplanet", "tanita"),        ("Sample Scale", "com.example.scale")),
    (("autosleep", "tantsissa"),        ("Sample Sleep Tracker", "com.example.sleeptracker")),
    (("muse", "interaxon"),             ("Sample Mindfulness Device", "com.example.mindfulness")),
]
_ANONYMIZE_FALLBACK = ("Sample Unknown Device", "com.example.unknown")


def _anonymize_source(src: dict, *, warn: "list[str]") -> dict:
    """1 つの ``source`` (``{"name": ..., "identifier": ...}``) を anonymize.

    ``identifier`` が ``com.apple.health.<UUID>`` 形式なら UUID を 0 化.
    それ以外は ``_ANONYMIZE_SOURCES`` の substring マッチでマップ. 未知なら
    fallback に置き換えて warning に追加 (呼び出し側で出力).
    """
    name = str(src.get("name", "")).lower()
    identifier = str(src.get("identifier", ""))
    haystack = name + " " + identifier.lower()

    if identifier.startswith("com.apple.health."):
        # HealthKit デバイス UUID: identifier は固定 placeholder, name はマップ
        for patterns, (anon_name, _) in _ANONYMIZE_SOURCES:
            if any(p in haystack for p in patterns):
                return {"name": anon_name, "identifier": f"com.apple.health.{_ANON_HEALTHKIT_UUID}"}
        warn.append(f"未知の HealthKit source: name={src.get('name')!r}")
        return {"name": _ANONYMIZE_FALLBACK[0], "identifier": f"com.apple.health.{_ANON_HEALTHKIT_UUID}"}

    for patterns, (anon_name, anon_id) in _ANONYMIZE_SOURCES:
        if any(p in haystack for p in patterns):
            return {"name": anon_name, "identifier": anon_id}

    warn.append(f"未知の source: name={src.get('name')!r}, identifier={identifier!r}")
    return {"name": _ANONYMIZE_FALLBACK[0], "identifier": _ANONYMIZE_FALLBACK[1]}


def _anonymize_payload(payload: dict, *, warn: "list[str]") -> dict:
    """``payload['data'][*]['sources'][*]`` を anonymize した新 dict を返す.

    payload 自体は変更せず、必要な部分だけ深い copy を作る.
    数値・timestamp・metric 名は parser 検証用に keep.
    """
    data = payload.get("data", [])
    if not isinstance(data, list):
        return dict(payload)
    new_data = []
    for record in data:
        if not isinstance(record, dict):
            new_data.append(record)
            continue
        sources = record.get("sources")
        if not isinstance(sources, list):
            new_data.append(record)
            continue
        new_record = dict(record)
        new_record["sources"] = [
            _anonymize_source(s, warn=warn) if isinstance(s, dict) else s
            for s in sources
        ]
        new_data.append(new_record)
    return {**payload, "data": new_data}


def _sample_head(data: "List[dict]", limit: int) -> "List[dict]":
    return list(data[:limit])


def _sample_tail(data: "List[dict]", limit: int) -> "List[dict]":
    return list(data[-limit:])


def _sample_evenly(data: "List[dict]", limit: int) -> "List[dict]":
    """等間隔サンプリング。len<=limit ならそのまま返す。

    各区間の**中央点** (``(i + 0.5) * step``) を拾うので head / tail bias がない。
    例: len=60000, limit=100 → step=600、サンプル index は [300, 900, 1500, ..., 59700]。
    """
    n = len(data)
    if n <= limit:
        return list(data)
    step = n / limit
    return [data[int((i + 0.5) * step)] for i in range(limit)]


_SAMPLERS = {
    "head": _sample_head,
    "tail": _sample_tail,
    "evenly": _sample_evenly,
}


def sample_payload(payload: dict, limit: int, mode: str) -> dict:
    """``payload`` の ``data`` 配列を間引いた新しい dict を返す。

    ``payload`` 自体は変更しない (冪等性確保 + 呼び出し側のミスを防ぐ)。
    ``data`` 以外のキー (``metric`` / ``date`` / ``source`` 等) はそのまま引き継ぐ。
    """
    sampler = _SAMPLERS[mode]
    data = payload.get("data", [])
    if not isinstance(data, list):
        # スキーマ違反は生のまま返す (warning を呼び出し側で出すなど下位判断)
        return dict(payload)
    sampled = sampler(data, limit)
    return {**payload, "data": sampled}


def _process_date(
    date_str: str,
    src_root: Path,
    dst_root: Path,
    limit: int,
    mode: str,
    anonymize: bool,
) -> bool:
    """1 日分の間引き処理。成功なら ``True``、src_dir 非存在なら ``False`` を返す。

    再生成時の **stale fixture 残留防止** のため、dst_dir の既存 ``*.json`` を
    一旦全削除してから書き直す (前回存在したが今回は src 側から消えたファイルも
    確実に掃除される)。README も同ディレクトリに置かれているので JSON だけ対象。
    """
    src_dir = src_root / date_str
    if not src_dir.is_dir():
        print(
            f"エラー: {src_dir} が見つかりません。事前に "
            f"`python3 -m ihealth --date {date_str}` でアーカイブを作成してください。",
            file=sys.stderr,
        )
        return False

    dst_dir = dst_root / date_str
    dst_dir.mkdir(parents=True, exist_ok=True)

    # stale fixture クリーンアップ (JSON だけ消す。README 等の周辺ファイルは残す)
    for old in dst_dir.glob("*.json"):
        old.unlink()

    anon_label = "anonymize=ON" if anonymize else "anonymize=OFF (DO NOT COMMIT)"
    print(f"date={date_str}: src={src_dir} → dst={dst_dir} (mode={mode}, limit={limit}, {anon_label})")
    src_files = sorted(src_dir.glob("*.json"))
    if not src_files:
        print(
            f"エラー: {src_dir} に .json が 1 つもありません。",
            file=sys.stderr,
        )
        return False
    warn: "list[str]" = []
    for src in src_files:
        payload = json.loads(src.read_text(encoding="utf-8"))
        sampled = sample_payload(payload, limit, mode)
        if anonymize:
            sampled = _anonymize_payload(sampled, warn=warn)
        dst = dst_dir / src.name
        # 末尾改行を固定 (git diff でのノイズ減らし、エディタの挙動差吸収)
        dst.write_text(
            json.dumps(sampled, ensure_ascii=False, indent=_INDENT) + "\n",
            encoding="utf-8",
        )
        before = len(payload.get("data", []))
        after = len(sampled.get("data", []))
        print(f"  {src.name}: {before} → {after} records")
    if warn:
        print(f"warning: 未知の source が {len(warn)} 件 fallback にマップされました:", file=sys.stderr)
        for w in dict.fromkeys(warn):  # uniq
            print(f"  - {w}", file=sys.stderr)
        print("  (新規 source なら scripts/make_fixtures.py の _ANONYMIZE_SOURCES に追加してください)", file=sys.stderr)
    return True


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make_fixtures.py",
        description="data/health 配下を間引いて tests/fixtures/ に保存する",
    )
    parser.add_argument(
        "dates",
        nargs="+",
        metavar="YYYY-MM-DD",
        help="対象日付 (複数指定可、例: 2026-04-22 2026-04-23)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=_SAMPLE_MAX,
        help=f"各ファイルの data 配列を切り詰める最大件数 (既定: {_SAMPLE_MAX})",
    )
    parser.add_argument(
        "--mode",
        choices=sorted(_SAMPLERS.keys()),
        default="evenly",
        help="サンプリング方式 (既定: evenly)",
    )
    parser.add_argument(
        "--no-anonymize",
        action="store_true",
        help=(
            "device UUID / source identifier / source name の anonymize を無効化. "
            "ローカル debug 専用. 本オプションで生成した fixture は git に commit "
            "してはいけない (個人 device fingerprint が漏洩する)."
        ),
    )
    args = parser.parse_args(argv)

    if args.limit <= 0:
        print("エラー: --limit は 1 以上を指定してください", file=sys.stderr)
        return 2

    project_root = Path(__file__).resolve().parent.parent
    src_root = project_root / "data" / "health"
    dst_root = project_root / "tests" / "fixtures" / "sample_health_export"

    anonymize = not args.no_anonymize
    if not anonymize:
        print(
            "警告: --no-anonymize 指定. 生成 fixture は実 device UUID と "
            "アプリ identifier を含みます. git commit しないでください.",
            file=sys.stderr,
        )

    failed: "list[str]" = []
    for d in args.dates:
        ok = _process_date(d, src_root, dst_root, args.limit, args.mode, anonymize)
        if not ok:
            failed.append(d)
    if failed:
        print(
            f"\nエラー: 以下の日付で fixture 生成に失敗しました: {failed}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
