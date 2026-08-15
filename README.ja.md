# kbd-signal

[English](README.md) | [日本語](README.ja.md)

Claude Code / Codex / Grok / Cursor のステータス(承認待ち・タスク完了・エラー。Cursor は完了通知のみ)を **VIA 対応キーボードのバックライト演出**で通知する Windows / macOS 用 CLI(デフォルト設定は Keychron K8 Pro)。

純正ファームウェアのまま、VIA raw HID プロトコル(usage page `0xFF60`)で RGB マトリクスを直接制御する。ファームウェア書き換え不要(v2/v3 プロトコルは自動判別)。

## 演出

| 状態 | トリガー | 演出 |
|------|---------|------|
| `waiting` | Claude Code / Codex の承認ダイアログ表示(`PermissionRequest` hook)、Grok の承認ダイアログ表示(`Notification` / `permission_prompt`) | オレンジのブリージング |
| `done` | メインターン完了(`Stop` hook。Cursor は `stop` の `status: completed`) | グリーン単色 5 秒 → 自動復元 |
| `error` | 手動 `kbd-signal set error` | レッドの高速ブリージング |

通知前に現在の設定(effect/speed/brightness/color)をスナップショットし、通知後に復元する。**EEPROM には一切書き込まない**(RAM のみ変更)ため、電源再投入で必ずユーザー設定に戻る。

### 復元モード(状態ディレクトリの `config.json`)

状態ディレクトリは Windows が `%LOCALAPPDATA%\kbd-signal`、macOS が `~/Library/Application Support/kbd-signal`。

```json
{"restore": "off"}
```

- `"baseline"`(デフォルト): 通知前のエフェクト・明るさに戻す
- `"off"`: 明るさ 0 で消灯に戻す(普段バックライトを消して使う人向け)。effect/color/speed はスナップショットを書き戻すので、Fn で点灯させたときは自分の設定が出る

なお Fn キーによるバックライトのオン/オフ状態(enable フラグ)は VIA プロトコルから読み書きできないため、baseline モードでは「消灯していた」ことまでは復元できない。消灯運用なら `"off"` を使う。

### 各状態の見え方(`config.json` の `states`)

状態ごとに色・速度・明るさを指定する。ブロックを書かなければ従来どおりのオレンジ / 緑 / 赤で、変えたいフィールドだけ上書きすればよい。

```json
{
  "states": {
    "waiting": {"effect": "solid", "hue": 0},
    "done": {"effect": "solid", "hue": 85}
  }
}
```

`effect` は生の番号ではなく `device.effects` のキー名で指定する。番号が何を意味するかは機種ごとに違うので、名前で書いておけば有効アニメーション一覧の異なるボードへ移しても同じ `states` がそのまま通る。既定に無いアニメーションを使うときは `effects` に足して(`"effects": {"solid": 1, "breathing": 2, "rainbow": 12}`)、その名前をここで指定する。

hue は QMK のホイールで赤 0 / オレンジ 21 / 緑 85。`hue` / `sat` / `speed` / `brightness` はいずれも 0-255。範囲外の値・未知の effect 名・状態名の綴り間違いは、その箇所を名指ししたエラーで拒否する。このブロックは手編集しかされないので、黙って既定値に戻すと「設定しても何も起きない」に見えてしまうため。

## 制約

- **背面スイッチ Cable(有線)時のみ動作**。BT モードではケーブルを挿していても raw HID インターフェースが列挙されない(実機確認済み)
- macOS では追加の権限やライブラリは不要(`hidapi` の macOS wheel は IOKit バックエンドで自己完結。`brew install hidapi` は不要)
- キーボード未接続時、hook 系コマンド(`hook` / `set` / `restore`)は exit 0 で静かに no-op(hooks を絶対にブロックしない)。診断系(`setup` / `export` / `detect` / `test` / `raw-effect`)は未検出を報告して exit 1
- **VIA アプリ / Keychron Launcher と同時使用しない**(raw HID 書き込みが競合する)
- Codex はライフサイクルフック対応版が必要。`codex features list` で `hooks` が有効か確認できる
- Grok はライフサイクルフック搭載の Grok Build が必要(1.0.3 で検証済み)。grok セッション内の `/hooks` で確認できる
- Cursor 対応は完了通知のみ(ターン完了で緑)。Cursor の hooks API に承認待ちイベントが無いため `waiting` は出せない(3.16.17 で検証済み)
- Claude Code / Codex / Grok / Cursor の複数セッションとサブエージェントを同時に追跡する。1件でも承認待ちが残っている間はオレンジを維持する

## プラットフォーム対応

| OS | ステータス | 実機検証 |
|----|-----------|---------|
| Windows | ✅ 対応 | Keychron K8 Pro(デフォルト)・実機 |
| macOS | ✅ 対応 | Keychron Q1 HE 8K・実機¹ |
| Linux | 🧪 ベストエフォート | CI のみ・実機検証なし |

¹ macOS の raw HID(`0xFF60` の列挙 / open / プロトコル判別 + Claude Code hook `waiting → done → restore`)は **Q1 HE 8K** で実機検証済み。**デフォルト機種の K8 Pro は macOS での set/restore ラウンドトリップが未検証** — プロトコル層は(検証済みの)Windows と共通なので動作する見込みだが未確認(動作報告歓迎)。

Linux は macOS と同じ POSIX 経路で動作し CI でも検証しているが、実機検証はない。PyPI classifier は Windows / macOS のみを記載する。

## インストール

推奨は [pipx](https://pipx.pypa.io/)。隔離環境にインストールされ、`kbd-signal` コマンドが PATH に載る(フックコマンドの要件とそのまま一致する):

```
pipx install kbd-signal
```

**Windows** — pipx が未導入の場合は先にセットアップする(初回のみ):

```powershell
py -m pip install --user pipx
py -m pipx ensurepath   # 実行後、新しいターミナルを開く
```

素の pip(`py -m pip install .`)でも動くが、その場合フックは**インストール先と同じインタプリタ**で `py -m kbd_signal hook claude` と呼ぶこと。

**macOS** — pipx が無ければ Homebrew で入れる(`brew install pipx && pipx ensurepath`)。あとは `pipx install kbd-signal`。`hidapi` の wheel は IOKit バックエンドで自己完結するため **`brew install hidapi` は不要**。設定・状態・ログは `~/Library/Application Support/kbd-signal/` に置かれる。

## 使い方

```
kbd-signal setup                 # 新しい機種の初期設定(対話)
kbd-signal export                # docs/devices + examples のプリセット雛形を出力
kbd-signal detect                # デバイス検出・現在の設定表示
kbd-signal set <waiting|done|error>
kbd-signal restore [--after N] [--gen G]
kbd-signal test                  # 全演出を順に再生
kbd-signal raw-effect <n>        # effect index 調査用
kbd-signal hook claude           # Claude Code hooks 用(stdin JSON)
kbd-signal hook codex [<json>]   # Codex hooks(stdin) / 旧 notify(argv) 用
kbd-signal hook grok             # Grok Build hooks 用(stdin JSON)
kbd-signal hook cursor           # Cursor hooks 用(stdin JSON・完了通知のみ)
```

### トラブルシューティング

restore のたびに演出色が戻ってくる(または Fn 点灯で演出色が出る)場合は、キーボードの電源を入れ直す。EEPROM には一切書き込んでいないため、電源再投入で必ず本来のユーザー設定に戻る。

逆に `"baseline"` モードで、それまで点灯設定に戻っていたのに信号の合間が消灯に変わった場合は(`"off"` モードでは消灯が正常動作)、snapshot が信号残骸と判定されて baseline が破棄された可能性が高い(残骸をユーザー設定として書き戻さないためのガード)。kbd-signal は過去に捕捉済みの「信号パターンに一致しなかった設定」へフォールバックする。それがまだ無い場合(アップグレード直後・新規導入時)は、電源を入れ直すか別の点灯パターンに切り替えて自分の設定を実際に表示させると、次の信号開始時に捕捉されて以後持ち越される。Fn 点灯だけでは不十分(残骸の輝度が上がるだけのことがある)。

## エージェント連携

### Claude Code (user scope `settings.json`)

`PermissionRequest` / `PostToolUse` / `Stop` / `SessionEnd` の各 hook に同一コマンドを登録(イベント名で内部振分):

```json
{"type": "command", "command": "kbd-signal hook claude", "timeout": 5}
```

そのまま使える例: [examples/claude-hooks.json](examples/claude-hooks.json) の `hooks` オブジェクトが上記4イベントを網羅している。`settings.json` の `hooks` へマージする(既存エントリは上書きしない)。

(pipx インストール時。素の pip の場合はインストール先インタプリタに合わせて `py -m kbd_signal hook claude` にする)

**プログラム位置にファイルパスを書かないこと。** フックコマンドは cmd と POSIX シェルのどちらで実行されるか環境依存で、バックスラッシュパスは POSIX シェルにエスケープとして食われ、フォワードスラッシュパスは cmd に "Access is denied" で拒否される — どちらも**無音で失敗**する(Windows 11 実測)。PATH 解決名(`kbd-signal` / `py -m kbd_signal`)なら両方で動く。アイドル時のエントリは軽量(hidapi DLL は遅延ロード)なので、PostToolUse のような高頻度フックも同じコマンドでよい。

この cmd/POSIX 問題は Windows 固有。macOS / Linux ではプログラム位置に絶対パスを書いても安全なので、フックが動く環境の PATH に `kbd-signal` が無い場合は絶対パスを使う(pipx はシムを `~/.local/bin/kbd-signal` に置く)。

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

`state.json`では所有者を`製品 / session_id / agent_id`の組み合わせで管理する。Claude Code / Codex / Grok / CursorのIDが同じでも衝突せず、メインセッションの完了は別製品・別セッションの承認待ちを解除しない。状態ファイルの読み書きはプロセス間ロックで直列化する。

ロールバックする場合は、`~/.codex/hooks.json`から上記のkbd-signalコマンドを持つイベントだけを削除してCodexを再起動する。`notify`は変更していないため、デスクトップアプリ側の通知設定はそのまま残る。

### Grok(v1.2.0〜)

Grok Build(xAI の `grok` CLI)は Claude Code 互換のライフサイクルフックを備えており、kbd-signal は3つ目のソースとしてこれを読む。grok 1.0.3 で検証済み。

1. [examples/grok-hooks.json](examples/grok-hooks.json) を `~/.grok/hooks/kbd-signal.json` として保存する。グローバルフックは常に trusted なので、プロジェクト単位の trust 手順は不要
2. grok を起動して `/hooks` を開き、Hooks タブに kbd-signal のエントリが並ぶことを確認する。編集後は同タブの `r` で再読込できる(再起動不要)
3. 承認が必要な操作を行い、待機中のオレンジと承認後の復元を確認する

登録するコマンドは全イベント共通:

```json
{"type": "command", "command": "kbd-signal hook grok", "timeout": 5}
```

#### Claude Code との違い

- `PermissionRequest` イベントが存在しない。承認待ちは `Notification` の type `permission_prompt` で届く(matcher を持つのはこのエントリだけ)。現時点で点灯するのはツール承認のプロンプトだけで、plan のレビュー待ちなど他の注意待ちは別の notification type を持ち、まだ通知対象にしていない
- **matcher は正規表現**。Claude の `"*"` はここでは不正な正規表現になる。全部にマッチさせたいときは matcher を省略する
- payload は camelCase(`hookEventName` / `sessionId`)、イベント値は小文字スネーク(`"stop"`)。`kbd-signal hook grok` が内部語彙へ変換し、Grok セッションは独自の `grok:` owner を持つ
- `Stop` は2回発火する。ターン完了時(`reason: "end_turn"`)と、セッション終了時の観測専用の1回(実測: `reason: "shutdown"`)。グリーンが出るのは `end_turn` のときだけで、終了時の分は古い承認待ちの掃除に使う
- Grok の `Stop` フックは blocking gate で、timeout のデフォルトは 600 秒。example は `timeout: 5` を明示し、フックは stdout に何も書かないため、ランプがターン終了を遅らせることはない
- Esc / Ctrl+C の中断では `Stop` が発火しない。残った承認待ちは同セッションの次のプロンプトか、1時間の TTL で解除される
- `PostToolUseFailure` と `PermissionDenied` を登録するのは、Grok が失敗・拒否したツールに `PostToolUse` を出さないため。無いと拒否後もオレンジが残る
- `StopFailure`(API エラーによるターン終了)は色を出さずにそのセッションの承認待ちを解除する
- Grok のサブエージェントは独立したセッションとして動く(1.0.3 実測)ため、子の承認待ちも同じセッション単位の解除で掃除される。なお grok のサブエージェントはデフォルト無効(`GROK_SUBAGENTS`)
- ヘッドレス実行(`grok -p`)でもフックは発火するので、スクリプトからの grok 呼び出しでも完了時に同じグリーンが出る。ノイズに感じる場合は自分のコピーから `Stop` エントリを外す

#### Claude 設定のスキャン

Grok はデフォルトで `~/.claude/settings.json` のフックも読むため、既存の `kbd-signal hook claude` エントリは Grok セッション内でも発火する。これは設計上の no-op で、Claude 入口はスネークケースのキーを探すので Grok の camelCase payload からは `session_id` が見つからず、ログを1行書いて exit 0 する(実測)。Grok セッションに `claude:` owner が作られることはなく、二重通知も起きない。`~/.grok/config.toml` に `[compat.claude] hooks = false` を書けばこのログは消えるが、Grok 内の **Claude フック全部**が止まるので、切る前に影響を確認すること。

ロールバックは `~/.grok/hooks/kbd-signal.json` を削除して `/hooks` の `r` で再読込(または grok を再起動)。

### Cursor(v1.3.0〜・完了通知のみ)

Cursor の hooks API(beta、Cursor 3.16.17 で実測)には**「エージェントが承認を待っている」ことを通知するイベントが存在しない**。公式フォーラムに機能要望が2件オープンしているが([166947](https://forum.cursor.com/t/fire-a-hook-when-agent-waits-for-command-tool-approval/166947) / [159912](https://forum.cursor.com/t/expose-agent-approval-waiting-state-via-hooks-cli-events/159912))、現状で最も近い `beforeShellExecution` 系は自動許可される実行でも毎回発火するため、waiting に流用すると「ツール実行中ずっとオレンジ」になってしまう。そのため Cursor 対応は他の3製品より意図的に狭い: **完了したターンで緑が出るだけ**。オレンジは出ない。Cursor が承認イベントを出したら `waiting` を追加する。

導入: [examples/cursor-hooks.json](examples/cursor-hooks.json) の `stop` エントリ1つを `~/.cursor/hooks.json` にマージする(トップレベルに `"version": 1` が必要。既存の hooks は上書きしない)。次のセッションから有効になる。

```json
{"command": "kbd-signal hook cursor", "timeout": 5}
```

登録が `stop` だけなのは意図的な設計。Cursor の hooks は同期・blocking で、掃除すべき waiting が存在しない以上、ライフサイクルイベントを登録しても「毎プロンプト送信に Python 起動を挟むだけ」になるため。

3.16.17 での実測に基づく挙動:

- 正常に完了したターン(`status: "completed"`)で緑 5 秒 → 復元。中断・失敗したターンでは何も出ない
- kbd-signal は stdout に何も書かない。Cursor は `stop` フックの stdout を JSON として読み、`followup_message` があるとエージェントを再開させるため、ここは特に重要。裏返すと、**別の stop フックが `followup_message` を返す構成では緑の後にエージェントが再開する**(Claude の stop gate と同種の注意点)
- Cursor は設定でサードパーティ取り込みを有効にすると `~/.claude/settings.json` の hooks も実行する。その場合 `kbd-signal hook claude` エントリは Cursor セッション内でも発火するが、小文字の `"stop"` は Claude の分岐に一致しないため状態は変化しない(実測・ログ1行のみ)。cursor エントリ導入後も二重通知にはならない
- 逆方向のスキャンもある: Grok は `~/.cursor/hooks.json` を読むため、`kbd-signal hook cursor` は Grok セッション内でも Grok payload で発火する — こちらも封筒が違うので no-op(テストでピン済み)
- `cursor-agent`(CLI)は同じ hooks ファイルを読むが発火イベントはサブセット。kbd-signal の cursor 経路は IDE でのみ実機検証済み

ロールバックは `~/.cursor/hooks.json` から `kbd-signal hook cursor` を呼ぶ `stop` エントリだけを削除する。

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
    "reset_on_effect": false,
    "effects": {"solid": 1, "breathing": 2}
  }
}
```

新しい機種での手順: **`kbd-signal setup`** を実行してください。デバイスの選択、設定済みの VIA v3 カスタムチャネルが実際にそのボードを駆動しているかの検証、`reset_on_effect` が必要かの実測までを自動で行い、最後に「どの effect 番号が点きっぱなしで、どれが明滅するか」をキーボードを見て答えてもらいます。ファームの有効アニメーション一覧は raw HID から読めないため、この2つの番号だけは目視でしか決められません。回答後に `device` ブロックを書き込みます(既存の `config.json` は `config.json.bak` に退避)。最後に `kbd-signal test` で確認。

チャネル検証に失敗した場合は、機種の VIA 定義にある `id_qmk_rgb_matrix_channel` の値を `config.json` の `v3_channel` に設定して `setup` を再実行してください。`setup` はその値を読みます。チャネルを推測して探索することはしません — 未対応チャネルへの書き込みは、そのファームでは別の意味を持つ値 id に着弾し得るためです。

`setup` は signal 表示中には実行を拒否します(終了時に元へ戻すため、現在の照明を退避する必要があるので)。

自分の機種が動いたら **`kbd-signal export`** を実行してください。機種ページを構成する2ファイル(`examples/config.<機種>.json` と `docs/devices/<機種>.md` の雛形)を、検出値を埋めた状態で出力します。人間しか書けない項目は `TODO` で明示されるので、そこを埋めて PR を出せば、同じキーボードを使う次の人が同じ作業を繰り返さずに済みます。デバイスからは読み出すだけなので、`setup` と違っていつでも実行できます。

手作業でやる場合: `kbd-signal detect --all` で VID/PID を調べて設定 → `kbd-signal raw-effect <n>` で solid/breathing の番号を特定して `effects` に設定 →(VIA v3 機なら)`v3_channel` を機種の VIA 定義に合わせる → `kbd-signal test`。RGB 非搭載機(単色バックライト)は色で状態を区別する設計のため対象外。

一部のファームは、エフェクト変更の約 50〜150ms 後に color(赤)と brightness(全開)をリセットします。多くの機種では起きないため、これは機種ごとのオプトイン方式の workaround です。`done` が赤くフラッシュ/固着する機種でのみ `"reset_on_effect": true` を設定してください。有効化すると、リセット窓の間 LED を暗く保ったまま色を確定させます。(既知の該当機種の一例が Keychron Q1 HE 8K です。)

### 対応確認済みデバイス

実機で確認済みのプリセット。各ページに VID/PID・プロトコル・エフェクト番号・注意点を記載。リンク先 config の `device` ブロックを `config.json` にコピーして使う。

- **Keychron Q1 HE 8K** — [詳細](docs/devices/keychron-q1-he-8k.ja.md) · [`config.q1-he-8k.json`](examples/config.q1-he-8k.json)

## License

MIT
