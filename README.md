# kbd-signal

Claude Code / Codex のステータス(承認待ち・タスク完了・エラー)を **Keychron K8 Pro のバックライト演出**で通知する Windows 用 CLI。

純正ファームウェアのまま、VIA v3 raw HID プロトコル(usage page `0xFF60`)で RGB マトリクスを直接制御する。ファームウェア書き換え不要。

## 演出

| 状態 | トリガー | 演出 |
|------|---------|------|
| `waiting` | Claude Code の承認ダイアログ表示(`PermissionRequest` hook) | オレンジのブリージング |
| `done` | ターン完了(`Stop` hook / Codex `agent-turn-complete`) | グリーン単色 5 秒 → 自動復元 |
| `error` | 手動 `kbd-signal set error`(v1) | レッドの高速ブリージング |

通知前に現在の設定(effect/speed/brightness/color)をスナップショットし、通知後に復元する。**EEPROM には一切書き込まない**(RAM のみ変更)ため、電源再投入で必ずユーザー設定に戻る。

## 制約

- **有線 USB 接続時のみ動作**。Bluetooth では raw HID が通らない(BT チップは標準キーレポートのみ中継)。ファームに `KEEP_USB_CONNECTION_IN_BLUETOOTH_MODE` があるため、BT モードでもケーブルを挿していれば制御できる可能性あり(要実機確認)
- キーボード未接続時は静かに no-op(hooks を絶対にブロックしない)
- **VIA アプリ / Keychron Launcher と同時使用しない**(raw HID 書き込みが競合する)
- Codex は承認待ちイベントを提供していないため turn-complete のみ対応

## インストール

```powershell
py -3.13 -m pip install -e .
```

## 使い方

```
kbd-signal detect                # デバイス検出・現在の設定表示
kbd-signal set <waiting|done|error>
kbd-signal restore [--after N] [--gen G]
kbd-signal test                  # 全演出を順に再生
kbd-signal raw-effect <n>        # effect index 調査用
kbd-signal hook claude           # Claude Code hooks 用(stdin JSON)
kbd-signal hook codex <json>     # Codex notify 用
```

## エージェント連携

### Claude Code (user scope `settings.json`)

`PermissionRequest` / `PostToolUse` / `Stop` / `SessionEnd` の各 hook に同一コマンドを登録(イベント名で内部振分):

```json
{"type": "command", "command": "<Scripts>\\kbd-signal.exe hook claude", "timeout": 5}
```

### Codex(v1 では未連携)

`~/.codex/config.toml` の `notify` は **Codex デスクトップアプリ自身の通知プログラムが既に占有**しており(`codex-computer-use.exe turn-ended`)、notify は 1 プログラムしか指定できない。バージョンハッシュ付きパスをラップするとアプリ更新で壊れるため v1 では見送り。

手動で有効化する場合は、両方を順に呼ぶラッパー(Python 推奨、cmd は JSON 引数のクォートが壊れる)を作り notify に指定する:

```toml
notify = ["py", "-3.13", "C:\\path\\to\\codex-notify-wrapper.py"]
```

なお Codex の notify は `agent-turn-complete` のみで承認待ちイベントは存在しない。

## プロトコルメモ(一次情報)

- Keychron/qmk_firmware `wireless_playground` ブランチ `keyboards/keychron/k8_pro/via_json/k8_pro_ansi_rgb.json` より:
  - カスタムチャネル id **3** = `id_qmk_rgb_matrix_channel`
  - value id: brightness=1, effect=2, speed=3, color(hue,sat)=4
  - effect index: None=0, **Solid Color=1, Breathing=2**, … Solid Splash=22
- パケット: `[report_id 0x00, cmd, channel, value_id, data...]`。cmd: set=0x07, get=0x08, save=0x09(**save は使用しない**)
- VID `0x3434`。PID は配列で異なるため VID + usage page で検出
