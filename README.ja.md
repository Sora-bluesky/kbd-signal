# kbd-signal

[English](README.md) | [日本語](README.ja.md)

Claude Code / Codex のステータス(承認待ち・タスク完了・エラー)を **Keychron K8 Pro のバックライト演出**で通知する Windows 用 CLI。

純正ファームウェアのまま、VIA raw HID プロトコル(usage page `0xFF60`)で RGB マトリクスを直接制御する。ファームウェア書き換え不要(v2/v3 プロトコルは自動判別)。

## 演出

| 状態 | トリガー | 演出 |
|------|---------|------|
| `waiting` | Claude Code の承認ダイアログ表示(`PermissionRequest` hook) | オレンジのブリージング |
| `done` | ターン完了(`Stop` hook / Codex `agent-turn-complete`) | グリーン単色 5 秒 → 自動復元 |
| `error` | 手動 `kbd-signal set error`(v1) | レッドの高速ブリージング |

通知前に現在の設定(effect/speed/brightness/color)をスナップショットし、通知後に復元する。**EEPROM には一切書き込まない**(RAM のみ変更)ため、電源再投入で必ずユーザー設定に戻る。

### 復元モード(`%LOCALAPPDATA%\kbd-signal\config.json`)

```json
{"restore": "off"}
```

- `"baseline"`(デフォルト): 通知前のエフェクト・明るさに戻す
- `"off"`: 明るさ 0 で消灯に戻す(普段バックライトを消して使う人向け)。effect/color/speed はスナップショットを書き戻すので、Fn で点灯させたときは自分の設定が出る

なお Fn キーによるバックライトのオン/オフ状態(enable フラグ)は VIA プロトコルから読み書きできないため、baseline モードでは「消灯していた」ことまでは復元できない。消灯運用なら `"off"` を使う。

## 制約

- **背面スイッチ Cable(有線)時のみ動作**。BT モードではケーブルを挿していても raw HID インターフェースが列挙されない(実機確認済み)
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

## プロトコルメモ(一次情報+実機確認)

- **出荷ファームは VIA protocol 9(v2)**(2026-07 実機で確認)。v2/v3 は open 時にコマンド 0x01 で自動判別する
  - v2: `[report_id 0x00, cmd, value_id, data...]`(チャネルなし)。value id: brightness=0x80, effect=0x81, speed=0x82, color(hue,sat)=0x83
  - v3(protocol >= 11、wireless_playground 世代): `[report_id 0x00, cmd, channel=3, value_id, data...]`。value id: brightness=1, effect=2, speed=3, color=4
- cmd: set=0x07, get=0x08, save=0x09(**save は使用しない**)
- effect index は新旧ファーム共通(info.json の animations 一覧が同一): None=0, **Solid Color=1, Breathing=2**, … Solid Splash=22。実機の Cycle Left Right=5 で整合確認済み
- Fn のバックライト on/off(enable フラグ)は VIA から読めない → 復元モード `"off"` で対応
- VID `0x3434`。PID は配列で異なるため VID + usage page 0xFF60 で検出
- 実測: BT モード+ケーブル接続では USB 列挙はされる(`KEEP_USB_CONNECTION_IN_BLUETOOTH_MODE`)が 0xFF60 raw HID は出ない。背面スイッチ Cable が必須

## License

MIT
