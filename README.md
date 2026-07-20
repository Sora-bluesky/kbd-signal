# kbd-signal

[English](README.md) | [日本語](README.ja.md)

Turn a **VIA-compatible RGB keyboard's backlight into a status lamp** for AI coding agents on Windows (defaults target the Keychron K8 Pro). When Claude Code or Codex waits for your approval, your keyboard starts breathing orange — no need to watch the screen.

Works on **stock firmware** (no flashing) by speaking the VIA raw HID protocol directly.

## Signals

| State | Trigger | Effect |
|-------|---------|--------|
| `waiting` | Claude Code / Codex shows a permission dialog (`PermissionRequest` hook) | Orange breathing |
| `done` | Main turn finished (`Stop` hook) | Solid green for 5 s, then auto-restore |
| `error` | Manual `kbd-signal set error` | Fast red breathing |

Before signaling, the current lighting (effect / speed / brightness / color) is snapshotted and restored afterwards. **Nothing is ever written to EEPROM** (RAM-only changes), so a power cycle always returns the keyboard to your saved settings.

## Requirements & limitations

- Windows, Python 3.11+ (`hidapi` is the only dependency)
- Keychron K8 Pro connected via **USB cable with the rear switch set to "Cable"**. Raw HID is not available over Bluetooth — measured: in BT mode with the cable attached, the USB HID collections enumerate but the `0xFF60` raw interface does not
- When the keyboard is absent (BT mode, unplugged), the hook-facing commands (`hook`, `set`, `restore`) silently no-op with exit 0 — hooks are never blocked. Diagnostic commands (`detect`, `test`, `raw-effect`) report the missing device and exit 1
- Do not run the VIA app / Keychron Launcher at the same time (concurrent raw HID writes race)
- Codex requires a version with lifecycle hooks; run `codex features list` and confirm that `hooks` is enabled
- Concurrent Claude Code / Codex sessions and subagents are tracked independently; orange remains active while any approval is pending

## Install

Recommended: [pipx](https://pipx.pypa.io/) — installs into an isolated environment and puts `kbd-signal` on PATH, which is exactly what the hook commands need:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath   # then open a new terminal
pipx install kbd-signal
```

Plain pip also works (`py -m pip install .`); in that case invoke the hooks with the **same interpreter** you installed into: `py -m kbd_signal hook claude`.

## Usage

```
kbd-signal detect                # find the keyboard, show protocol & current lighting
kbd-signal set <waiting|done|error>
kbd-signal restore [--after N] [--gen G]
kbd-signal test                  # play all patterns, then restore
kbd-signal raw-effect <n>        # set a raw effect index (debug)
kbd-signal hook claude           # entry point for Claude Code hooks (JSON on stdin)
kbd-signal hook codex [<json>]   # Codex hooks (stdin) / legacy notify (argv)
```

### Restore mode (`%LOCALAPPDATA%\kbd-signal\config.json`)

```json
{"restore": "off"}
```

- `"baseline"` (default): restore the pre-signal effect and brightness
- `"off"`: restore to brightness 0 (for people who normally keep the backlight dark). The stored effect/color/speed are still written back, so waking the backlight with Fn shows your own settings

The Fn backlight on/off flag is not readable over VIA, which is why `"off"` exists.

## Claude Code integration

Register the same command for `PermissionRequest`, `PostToolUse`, `Stop`, and `SessionEnd` in your user-scope `settings.json` (events are dispatched internally by `hook_event_name`):

```json
{"type": "command", "command": "kbd-signal hook claude", "timeout": 5}
```

(pipx install — the `kbd-signal` shim is on PATH. With a plain pip install, use `py -m kbd_signal hook claude` instead, matching the interpreter you installed into.)

**Do not put a filesystem path in the program position.** Hook commands may run through either `cmd` or a POSIX shell: backslashed paths get eaten as escapes by the POSIX shell, and forward-slashed program paths fail under `cmd` with "Access is denied" — both silently, so the hook simply never signals (measured on Windows 11). PATH-resolved names (`kbd-signal`, `py -m kbd_signal`) work under both. The entry point is cheap when idle (the hidapi DLL is imported lazily), so the same command is fine for hot hooks like `PostToolUse`.

## Codex integration (since v0.3.0)

Use Codex lifecycle hooks. They are separate from the `notify` entry in `~/.codex/config.toml`, so **leave the existing `notify` command unchanged**.

1. Run `codex features list` and confirm that `hooks` is enabled
2. Merge the events from [examples/codex-hooks.json](examples/codex-hooks.json) into the user-level `~/.codex/hooks.json`; do not overwrite an existing file
3. Start the Codex CLI and choose `Review hooks` from the startup `Hooks need review` prompt, or open `/hooks`. Verify the source, event, and command before trusting them. Trust is tied to the hook definition hash, so review it again after any change
4. In a new session, trigger an approval and verify orange while waiting and restoration after approval

Every event uses the same command:

```json
{
  "type": "command",
  "command": "kbd-signal hook codex",
  "timeout": 5
}
```

The configuration uses:

- `PermissionRequest` to add a pending approval
- `PostToolUse` to release only the agent that completed its tool
- `Stop` to release the main session and signal completion only when no other session is waiting
- `SubagentStop` to clean up a child without flashing green for the whole task
- `SessionStart` / `UserPromptSubmit` to clean up stale entries for the same session after an interrupted run

Codex does not expose `SessionEnd`. If Codex is force-closed while an approval is pending and that session is never resumed, orange can remain active; run `kbd-signal restore` to recover.

The old `agent-turn-complete` notify payload remains supported for compatibility, but it cannot report approval waits and competes with the desktop app's notifier, so it is not recommended for new installations.

### Concurrent sessions

Owners are keyed by product, `session_id`, and `agent_id`. A main-session completion therefore cannot clear another Claude/Codex session or one of its subagents. Updates to `state.json` remain serialized by the existing interprocess lock.

To roll back, remove only the entries whose command invokes `kbd_signal hook codex` from `~/.codex/hooks.json`, then restart Codex. The desktop app's `notify` configuration remains untouched.

## Protocol notes (verified on hardware)

- Shipped K8 Pro stock firmware speaks **VIA protocol 9 (v2)** — not the v3 custom-channel layout found on the `wireless_playground` branch. The protocol is probed at open time (command `0x01`) and both layouts are supported:
  - v2: `[report_id 0x00, cmd, value_id, data...]`, value ids `0x80` brightness / `0x81` effect / `0x82` speed / `0x83` color (hue, sat)
  - v3 (protocol ≥ 11): `[report_id 0x00, cmd, channel=3, value_id, data...]`, value ids 1–4
- Commands: set `0x07`, get `0x08`, save `0x09` (**save is never used**)
- Effect indices are identical across firmware generations (`info.json` animation list matches): None=0, **Solid Color=1, Breathing=2**, … Solid Splash=22
- Device detection: VID `0x3434` + usage page `0xFF60` / usage `0x61` (PIDs differ per layout variant)

## Other keyboards (since v0.2.0)

The protocol layer is not K8 Pro specific: VIA v2 value ids are fixed by the VIA spec and Solid Color is always effect 1 in QMK. Point kbd-signal at another VIA-compatible RGB keyboard via `config.json`:

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

Workflow for a new board:

1. `kbd-signal detect --all` — list every raw-HID (0xFF60) device and copy its VID/PID into `config.json`
2. `kbd-signal raw-effect <n>` — step through effect indices until you find solid/breathing, then set `effects`
3. On a VIA v3 board, set `v3_channel` to the keyboard's `id_qmk_rgb_matrix` channel from its VIA definition (v2 boards ignore it)
4. `kbd-signal test`

Boards without RGB (single-color backlight) are out of scope — states are color-coded.

## License

MIT
