# Keychron Q1 HE 8K

[English](keychron-q1-he-8k.md) | [日本語](keychron-q1-he-8k.ja.md)

実機で確認済み。プリセット: [`examples/config.q1-he-8k.json`](../../examples/config.q1-he-8k.json) — `device` ブロックを `config.json` にコピーして使う。

## 設定

| 項目 | 値 | 備考 |
|------|-----|------|
| `vendor_id` | `0x3434` | Keychron |
| `product_id` | `0x1012` | この個体の PID — **配列により異なる場合あり**。下記の注記を参照 |
| `product_match` | `Q1 HE` | Link-KM ドックとキーボード本体を区別する |
| `v3_channel` | `3` | rgb_matrix カスタムチャネル |
| `effects` | `solid`=1, `breathing`=2 | K8 Pro 既定と同じ番号 |

- VIA プロトコル **13**(v3 カスタムチャネル)。チャネル 3 の value id は `brightness`=1 / `effect`=2 / `speed`=3 / `color`(hue, sat)=4。
- hue は QMK 標準の 0–255 ホイール(red=0 / yellow≈43 / green=85 / blue=170)。kbd-signal 組み込みの配色をそのまま使える。

### `product_id`(0x1012)について

`0x1012` は**この個体**が報告する値で、`kbd-signal detect --all` で確認したもの:

```
found: Keychron  Keychron Link-KM (VID=0x3434 PID=0xd026)
found: Keychron Keychron Q1 HE 8K (VID=0x3434 PID=0x1012)
```

Keychron は物理配列(ANSI / ISO / JIS)ごとに異なる PID を割り当てているため、手元の個体では別の値になることがある。`kbd-signal detect --all` を実行し、自分のキーボードに表示された PID を設定すること。なお `product_match: "Q1 HE"` だけでも本体とドックを区別できる(下記参照)ため、`product_id` を外して `product_match` のみに頼る運用も可能。

Keychron の公開定義と突き合わせたところ、PID には **2 系統** あり、配列オフセット(`ANSI` → …0 / `ISO` → …1 / `JIS` → …2)はどちらでも共通:

| 配列 | QMK ファーム¹ | 工場出荷 / Launcher ファーム² |
|------|--------------|------------------------------|
| ANSI | `0x0B10` | `0x1010` |
| ISO | `0x0B11` | `0x1011` |
| JIS | `0x0B12` | `0x1012` |

¹ [`Keychron/qmk_firmware` → `keyboards/keychron/q1_he/{ansi,iso,jis}_encoder/keyboard.json`](https://github.com/Keychron/qmk_firmware/tree/master/keyboards/keychron/q1_he)(VID `0x3434`。Q1 HE 8K は `q1_he` のファームを共有)。対応する VIA 定義: [`SRGBmods/QMK-Binaries` → `VIA_JSON/keychron`](https://github.com/SRGBmods/QMK-Binaries/tree/master/VIA_JSON/keychron)。
² [Keychron Launcher](https://www.keychron.com/pages/firmware-and-json-files-of-the-keychron-he-series-keyboards) 向けに出荷される工場ファーム。その PID は公開の QMK / the-via リポジトリには載っていない。このプリセットの `0x1012` は工場ファーム側の値 = JIS 枠で、検証した個体と一致する。

## ドッキングステーションの注意点

**Link-KM ドッキングステーション経由**で接続すると(付属品ではなく別途用意したドックで、この構成で検証した)、**このドックも `0x3434` の raw HID(`0xFF60`)デバイスとして列挙される**(PID `0xd026`)。既定の `product_match` である `K8` はドックにもキーボードにも一致しないため、検出がドック側を掴むことがあり(プロトコルにも値読み取りにも無応答)、全コマンドが失敗する。このプリセットでは `Q1 HE` で照合し(ドックは `Keychron Link-KM` を報告する)、`product_id` をキーボード本体に固定することで回避している。

## 接続

背面スイッチを **「Cable」** にして USB ケーブルで接続する。VIA アプリ / Keychron Launcher は同時起動しない(raw HID 書き込みが競合する)。
