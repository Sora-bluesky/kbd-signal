# kbd-signal

[English](README.md) | [日本語](README.ja.md)

Turn a **VIA-compatible RGB keyboard's backlight into a status lamp** for AI coding agents on Windows (defaults target the Keychron K8 Pro). When Claude Code stops and waits for your approval, your keyboard starts breathing orange — no need to watch the screen.

Works on **stock firmware** (no flashing) by speaking the VIA raw HID protocol directly.

## Signals

| State | Trigger | Effect |
|-------|---------|--------|
| `waiting` | Claude Code shows a permission dialog (`PermissionRequest` hook) | Orange breathing |
| `done` | Turn finished (`Stop` hook) | Solid green for 5 s, then auto-restore |
| `error` | Manual `kbd-signal set error` (v1) | Fast red breathing |

Before signaling, the current lighting (effect / speed / brightness / color) is snapshotted and restored afterwards. **Nothing is ever written to EEPROM** (RAM-only changes), so a power cycle always returns the keyboard to your saved settings.

## Requirements & limitations

- Windows, Python 3.11+ (`hidapi` is the only dependency)
- Keychron K8 Pro connected via **USB cable with the rear switch set to "Cable"**. Raw HID is not available over Bluetooth — measured: in BT mode with the cable attached, the USB HID collections enumerate but the `0xFF60` raw interface does not
- When the keyboard is absent (BT mode, unplugged), the hook-facing commands (`hook`, `set`, `restore`) silently no-op with exit 0 — hooks are never blocked. Diagnostic commands (`detect`, `test`, `raw-effect`) report the missing device and exit 1
- Do not run the VIA app / Keychron Launcher at the same time (concurrent raw HID writes race)
- Codex CLI is not integrated in v1: its single `notify` slot is already occupied by the Codex desktop app's own notifier (see README.ja.md for a manual wrapper recipe)

## Install

```powershell
py -3.13 -m pip install .
```

## Usage

```
kbd-signal detect                # find the keyboard, show protocol & current lighting
kbd-signal set <waiting|done|error>
kbd-signal restore [--after N] [--gen G]
kbd-signal test                  # play all patterns, then restore
kbd-signal raw-effect <n>        # set a raw effect index (debug)
kbd-signal hook claude           # entry point for Claude Code hooks (JSON on stdin)
kbd-signal hook codex <json>     # entry point for Codex notify
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
{"type": "command", "command": "py -3.13 -m kbd_signal hook claude", "timeout": 5}
```

**Do not put a filesystem path in the program position.** Hook commands may run through either `cmd` or a POSIX shell: backslashed paths get eaten as escapes by the POSIX shell, and forward-slashed program paths fail under `cmd` with "Access is denied" — both silently, so the hook simply never signals (measured on Windows 11). A PATH-resolved launcher (`py -3.13 -m kbd_signal ...`) works under both. The entry point is cheap when idle (the hidapi DLL is imported lazily), so the same command is fine for hot hooks like `PostToolUse`.

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
