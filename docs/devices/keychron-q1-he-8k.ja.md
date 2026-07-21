# Keychron Q1 HE 8K

[English](keychron-q1-he-8k.md) | [日本語](keychron-q1-he-8k.ja.md)

実機で確認済み。プリセット: [`examples/config.q1-he-8k.json`](../../examples/config.q1-he-8k.json) — `device` ブロックを `config.json` にコピーして使う。

## 設定

| 項目 | 値 | 備考 |
|------|-----|------|
| `vendor_id` | `0x3434` | Keychron |
| `product_id` | `0x1012` | **有線キーボード本体** — 下記のドッキングステーション注意点を参照 |
| `product_match` | `Q1 HE` | |
| `v3_channel` | `3` | rgb_matrix カスタムチャネル |
| `effects` | `solid`=1, `breathing`=2 | K8 Pro 既定と同じ番号 |

- VIA プロトコル **13**(v3 カスタムチャネル)。チャネル 3 の value id は `brightness`=1 / `effect`=2 / `speed`=3 / `color`(hue, sat)=4。
- hue は QMK 標準の 0–255 ホイール(red=0 / yellow≈43 / green=85 / blue=170)。kbd-signal 組み込みの配色をそのまま使える。

## ドッキングステーションの注意点

Q1 HE 8K には **Link-KM ドッキングステーションが付属し、これも `0x3434` の raw HID(`0xFF60`)デバイスとして列挙される**(PID `0xd026`。有線キーボード本体は `0x1012`)。既定の `product_match` である `K8` はどちらにも一致しないため、検出がドッキングステーション側を掴むことがあり(プロトコルにも値読み取りにも無応答)、全コマンドが失敗する。`product_id` を `0x1012` に固定するとこれを回避できる。

## 接続

背面スイッチを **「Cable」** にして USB ケーブルで接続する。VIA アプリ / Keychron Launcher は同時起動しない(raw HID 書き込みが競合する)。
