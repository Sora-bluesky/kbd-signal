# kbd-signal

[English](README.md) | [日本語](README.ja.md)

Claude Code / Codex のステータス(承認待ち・タスク完了・エラー)を **VIA 対応キーボードのバックライト演出**で通知する Windows 用 CLI(デフォルト設定は Keychron K8 Pro)。

純正ファームウェアのまま、VIA raw HID プロトコル(usage page `0xFF60`)で RGB マトリクスを直接制御する。ファームウェア書き換え不要(v2/v3 プロトコルは自動判別)。

## 演出

| 状態 | トリガー | 演出 |
|------|---------|------|
| `waiting` | Claude Code / Codex の承認ダイアログ表示(`PermissionRequest` hook) | オレンジのブリージング |
| `done` | メインターン完了(`Stop` hook) | グリーン単色 5 秒 → 自動復元 |
| `error` | 手動 `kbd-signal set error` | レッドの高速ブリージング |

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
- キーボード未接続時、hook 系コマンド(`hook` / `set` / `restore`)は exit 0 で静かに no-op(hooks を絶対にブロックしない)。診断系(`detect` / `test` / `raw-effect`)は未検出を報告して exit 1
- **VIA アプリ / Keychron Launcher と同時使用しない**(raw HID 書き込みが競合する)
- Codex はライフサイクルフック対応版が必要。`codex features list` で `hooks` が有効か確認できる
- Claude Code / Codex の複数セッションとサブエージェントを同時に追跡する。1件でも承認待ちが残っている間はオレンジを維持する

## インストール

推奨は [pipx](https://pipx.pypa.io/)。隔離環境にインストールされ、`kbd-signal` コマンドが PATH に載る(フックコマンドの要件とそのまま一致する):

```powershell
py -m pip install --user pipx
py -m pipx ensurepath   # 実行後、新しいターミナルを開く
pipx install git+https://github.com/Sora-bluesky/kbd-signal
```

素の pip(`py -m pip install .`)でも動くが、その場合フックは**インストール先と同じインタプリタ**で `py -m kbd_signal hook claude` と呼ぶこと。

## 使い方

```
kbd-signal detect                # デバイス検出・現在の設定表示
kbd-signal set <waiting|done|error>
kbd-signal restore [--after N] [--gen G]
kbd-signal test                  # 全演出を順に再生
kbd-signal raw-effect <n>        # effect index 調査用
kbd-signal hook claude           # Claude Code hooks 用(stdin JSON)
kbd-signal hook codex [<json>]   # Codex hooks(stdin) / 旧 notify(argv) 用
```

## エージェント連携

### Claude Code (user scope `settings.json`)

`PermissionRequest` / `PostToolUse` / `Stop` / `SessionEnd` の各 hook に同一コマンドを登録(イベント名で内部振分):

```json
{"type": "command", "command": "kbd-signal hook claude", "timeout": 5}
```

(pipx インストール時。素の pip の場合はインストール先インタプリタに合わせて `py -m kbd_signal hook claude` にする)

**プログラム位置にファイルパスを書かないこと。** フックコマンドは cmd と POSIX シェルのどちらで実行されるか環境依存で、バックスラッシュパスは POSIX シェルにエスケープとして食われ、フォワードスラッシュパスは cmd に "Access is denied" で拒否される — どちらも**無音で失敗**する(Windows 11 実測)。PATH 解決名(`kbd-signal` / `py -m kbd_signal`)なら両方で動く。アイドル時のエントリは軽量(hidapi DLL は遅延ロード)なので、PostToolUse のような高頻度フックも同じコマンドでよい。

### Codex(v0.3.0〜)

現行Codexのライフサイクルフックを使う。デスクトップアプリが使う`~/.codex/config.toml`の`notify`とは別経路なので、**既存の`notify`は変更しない**。

1. `codex features list`を実行し、`hooks`が有効であることを確認する
2. [examples/codex-hooks.json](examples/codex-hooks.json)の各イベントを、ユーザー単位の`~/.codex/hooks.json`へ追加する。既存ファイルがある場合は上書きせず、`hooks`オブジェクトへマージする
3. Codex CLIを起動し、起動時の`Hooks need review`から`Review hooks`を選ぶか、`/hooks`を開く。登録元・イベント・コマンドを確認して信頼する。信頼はフック定義のハッシュ単位なので、内容を変更した場合は再確認する
4. 新しいセッションで承認が必要な操作を行い、オレンジ点灯と承認後の復元を確認する

登録するコマンドは全イベント共通:

```json
{
  "type": "command",
  "command": "kbd-signal hook codex",
  "timeout": 5
}
```

使用するイベント:

- `PermissionRequest`: 承認待ちを追加
- `PostToolUse`: 実行を終えたエージェントの承認待ちだけを解除
- `Stop`: 同じメインセッション配下を解除し、他に待機がなければ完了を通知
- `SubagentStop`: 子エージェントの待機だけを解除。完了のグリーンは表示しない
- `SessionStart` / `UserPromptSubmit`: 異常終了などで同じセッションに残った古い待機を掃除

Codexには`SessionEnd`がないため、承認待ちの最中にアプリを強制終了し、そのセッションを再開しない場合はオレンジが残ることがある。その場合は`kbd-signal restore`で復旧する。

旧`notify`の`agent-turn-complete`入力も引き続き受け付ける。ただし、承認待ちを取得できず、デスクトップアプリの通知経路とも競合するため、新規設定には使わない。

### 複数セッションの扱い

`state.json`では所有者を`製品 / session_id / agent_id`の組み合わせで管理する。Claude CodeとCodexのIDが同じでも衝突せず、メインセッションの完了は別製品・別セッションの承認待ちを解除しない。状態ファイルの読み書きはプロセス間ロックで直列化する。

ロールバックする場合は、`~/.codex/hooks.json`から上記のkbd-signalコマンドを持つイベントだけを削除してCodexを再起動する。`notify`は変更していないため、デスクトップアプリ側の通知設定はそのまま残る。

## プロトコルメモ(一次情報+実機確認)

- **出荷ファームは VIA protocol 9(v2)**(2026-07 実機で確認)。v2/v3 は open 時にコマンド 0x01 で自動判別する
  - v2: `[report_id 0x00, cmd, value_id, data...]`(チャネルなし)。value id: brightness=0x80, effect=0x81, speed=0x82, color(hue,sat)=0x83
  - v3(protocol >= 11、wireless_playground 世代): `[report_id 0x00, cmd, channel=3, value_id, data...]`。value id: brightness=1, effect=2, speed=3, color=4
- cmd: set=0x07, get=0x08, save=0x09(**save は使用しない**)
- effect index は新旧ファーム共通(info.json の animations 一覧が同一): None=0, **Solid Color=1, Breathing=2**, … Solid Splash=22。実機の Cycle Left Right=5 で整合確認済み
- Fn のバックライト on/off(enable フラグ)は VIA から読めない → 復元モード `"off"` で対応
- VID `0x3434`。PID は配列で異なるため VID + usage page 0xFF60 で検出
- 実測: BT モード+ケーブル接続では USB 列挙はされる(`KEEP_USB_CONNECTION_IN_BLUETOOTH_MODE`)が 0xFF60 raw HID は出ない。背面スイッチ Cable が必須

## 他のキーボードで使う(v0.2.0〜)

`config.json` の `device` セクションで VID/PID・v3 チャネル・エフェクト番号を差し替えられる:

```json
{
  "restore": "off",
  "device": {
    "vendor_id": "0x3434",
    "product_id": null,
    "product_match": "K8",
    "v3_channel": 3,
    "effects": {"solid": 1, "breathing": 2}
  }
}
```

新しい機種での手順: `kbd-signal detect --all` で VID/PID を調べて設定 → `kbd-signal raw-effect <n>` で solid/breathing の番号を特定して `effects` に設定 →(VIA v3 機なら)`v3_channel` を機種の VIA 定義に合わせる → `kbd-signal test`。RGB 非搭載機(単色バックライト)は色で状態を区別する設計のため対象外。

## License

MIT
