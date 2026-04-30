# tests/fixtures/

テスト用の間引き **合成サンプルデータ** (Health Auto Export の出力構造に
合わせた anonymized fixtures)。`scripts/make_fixtures.py` で
`data/health/YYYY-MM-DD/*.json` (.gitignore 対象) を読み取り、
`data` 配列を代表的な件数に切り詰めたコピーを保存しているが、コミット
時には UUID / source identifier / source name を全て anonymize 済み:

- HealthKit デバイス UUID → `00000000-0000-0000-0000-000000000000`
- アプリ identifier → `com.example.{scale,sleeptracker,mindfulness}` 等
- デバイス name → `Sample Watch` / `Sample Scale` 等

数値 (歩数 / 心拍 / 体重 / 体脂肪率 / 睡眠) と秒精度の timestamp は
parser 集計ルールの回帰検証に必要なため意味のある値を保持しているので、
完全に de-identified された合成データではなく **「device fingerprint と
アプリ identifier だけ匿名化したサンプル」** という位置付け.

**Privacy 注意 (履歴):** 現在の作業ツリーは anonymize 済みだが、リポジトリ
の `git log` を遡ると anonymize 以前の commit (initial fixture 投入時) に
**実 device UUID やアプリ identifier が残存**している。これは将来的に
`git filter-repo` で削除する選択肢を残しているが、v0.1.0 リリース時点では
著者判断で keep されている。この点を厳密に避けたい fork は、作業ツリー
だけでなく history も rewrite して再公開してください.

**再生成時の注意:** `scripts/make_fixtures.py` は既定で `--anonymize` ON
で動き、commit 対象の fixture は常に anonymize 済みになる。`--no-anonymize`
は debug 専用で、生成された fixture を git commit してはならない.

## ディレクトリ構成

```
sample_health_export/
├── 2026-04-22/   # フル populate (13 メトリクスのうち 12 フィールドに値、瞑想のみなし)
└── 2026-04-23/   # 部分欠落 (昼寝・体重・体脂肪なし、瞑想あり)
```

## 採用日付の理由

| 日付 | なぜ fixture に選んだか |
| --- | --- |
| 2026-04-22 | 13 フィールドのうち **12** フィールドが populated (瞑想のみなし)。`sleep_hours` (夜主睡眠) と `nap_hours` (昼寝) の両方が同時に取れる構成で、sleep_analysis の JST end.hour 分岐を検証できる。`body_mass_kg` / `body_fat_percentage` 値も含まれる。 |
| 2026-04-23 | 部分欠落ケースのカバレッジ。昼寝なし (`nap_hours=None`)・体重計測なし (`body_mass_kg=None`) ・瞑想あり (`mindful_minutes`/`mindful_sessions` not None)。「データが無い日」の parser 挙動 (None フィールド) を検証する。 |

## 再生成方法

```bash
# fixture を最新のアーカイブから再生成 (既定 mode=evenly, limit=100)
# 事前に data/health/YYYY-MM-DD/ が埋まっている必要あり (python3 -m ihealth で生成)
/usr/bin/python3 scripts/make_fixtures.py 2026-04-22 2026-04-23

# 小さいサイズで再生成したい場合
/usr/bin/python3 scripts/make_fixtures.py 2026-04-22 2026-04-23 --limit 50

# head モード (先頭 N 件) を明示する場合
/usr/bin/python3 scripts/make_fixtures.py 2026-04-22 --mode head

# 実 device fingerprint を含む生 fixture (debug 専用、コミット禁止)
/usr/bin/python3 scripts/make_fixtures.py 2026-04-22 --no-anonymize
```

`--no-anonymize` 指定なしでは常に anonymize が走り、commit 対象の fixture
は安全になる. `--no-anonymize` で生成した fixture を git に push してはいけない.

再生成は **dst_dir の *.json を一旦全削除してから** 書き直すので、
入力側から消えたファイルの stale 残留は起こらない (README 等の周辺ファイルは保持)。

src_dir が存在しない / 中身が空 の日付は exit 1 で失敗する (成功扱いで古い fixture を温存しない)。

**間引きモード**:
- `evenly` (既定): 等間隔 N 件 + 各区間の中央点。head / tail bias なし。SUM 系集計の時系列偏り抑制に有効
- `head`: 先頭 N 件 (実装最小、時系列の先頭偏り)
- `tail`: 末尾 N 件 (末尾重視)

本リポジトリは `evenly --limit 100` で生成している。

## 間引きによる値の変化

間引きサンプル (`evenly --limit 100`) で `parse_all` を実行した結果は、
時系列 SUM 系 (step / distance / active_energy) で値が 50-60 倍ほど小さく
なるが、**型と None/not-None の挙動は維持される**ので parser の回帰
テストに使える。

時系列以外 (sleep / 体重 / 瞑想 / 体脂肪率 / 安静時心拍) は元データが
100 件未満なので間引き後も全件残り、値は変わらない。
型と非 None 判定は全フィールドで保持される。

## テストから読み込む方法

```python
from pathlib import Path
from datetime import date
from ihealth.parser import ParseContext, parse_all

FIX_ROOT = Path(__file__).resolve().parent / "fixtures" / "sample_health_export"

def test_parse_all_2026_04_22():
    ctx = ParseContext(target_date=date(2026, 4, 22), max_heart_rate=178.0)
    result = parse_all(FIX_ROOT / "2026-04-22", date(2026, 4, 22), ctx)
    assert result.step_count is not None
    assert result.nap_hours is not None  # 昼寝あり
    assert result.mindful_minutes is None  # 瞑想なし

def test_parse_all_2026_04_23():
    ctx = ParseContext(target_date=date(2026, 4, 23), max_heart_rate=178.0)
    result = parse_all(FIX_ROOT / "2026-04-23", date(2026, 4, 23), ctx)
    assert result.nap_hours is None  # 昼寝なし
    assert result.body_mass_kg is None  # 体重未計測
    assert result.mindful_minutes is not None  # 瞑想あり
```
