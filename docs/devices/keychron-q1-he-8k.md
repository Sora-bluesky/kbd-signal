# Keychron Q1 HE 8K

[English](keychron-q1-he-8k.md) | [日本語](keychron-q1-he-8k.ja.md)

Verified on real hardware. Preset: [`examples/config.q1-he-8k.json`](../../examples/config.q1-he-8k.json) — copy its `device` block into your `config.json`.

## Config

| Field | Value | Notes |
|-------|-------|-------|
| `vendor_id` | `0x3434` | Keychron |
| `product_id` | `0x1012` | this unit's PID — **may differ on your board**; see the note below |
| `product_match` | `Q1 HE` | prefers the keyboard over the Link-KM dock (not a hard filter — see below) |
| `v3_channel` | `3` | rgb_matrix custom channel |
| `reset_on_effect` | `true` | this board resets color/brightness after an effect change — see below |
| `effects` | `solid`=1, `breathing`=2 | same indices as the K8 Pro default |

- VIA protocol **13** (v3 custom channel). On channel 3 the value ids are `brightness`=1, `effect`=2, `speed`=3, `color`(hue, sat)=4.
- Hue uses the standard QMK 0–255 wheel (red=0, yellow≈43, green=85, blue=170), so kbd-signal's built-in colors need no remapping.
- `reset_on_effect` is `true` because this board (verified on real hardware) resets color to red and brightness to full ~50–150 ms **after** an effect change, which would otherwise leave `done` flashing/stuck red. With it on, kbd-signal holds the LEDs dark across that window and settles the color afterward. The flag is default-`false`, so the preset opts in for you.

### About `product_id` (0x1012)

`0x1012` is what **this unit** reports — verified with `kbd-signal detect --all`:

```
found: Keychron  Keychron Link-KM (VID=0x3434 PID=0xd026)
found: Keychron Keychron Q1 HE 8K (VID=0x3434 PID=0x1012)
```

Keychron assigns different PIDs per physical layout (ANSI / ISO / JIS), so your board may report a different value. Run `kbd-signal detect --all` and pin the PID it prints for your keyboard.

`product_match` is only a *preference*, not a hard filter: `find_device_path` (`kbd_signal/via.py`) returns the first candidate whose `product_string` contains the match, but **falls back to the first enumerated device when nothing matches**. With the current product strings (`Keychron  Keychron Link-KM` vs `Keychron Keychron Q1 HE 8K`), `product_match: "Q1 HE"` reliably selects the keyboard, so you *could* drop `product_id` — but if a firmware update changes the product string, the fallback could grab the dock instead. Pinning `product_id` is the dependable option; keep it set unless you have a reason not to.

Cross-checked against Keychron's public definitions, there are **two PID schemes** and the layout offset (`ANSI` → …0, `ISO` → …1, `JIS` → …2) holds in both:

| Layout | QMK firmware¹ | Factory / Launcher firmware² |
|--------|--------------|------------------------------|
| ANSI | `0x0B10` | `0x1010` |
| ISO | `0x0B11` | `0x1011` |
| JIS | `0x0B12` | `0x1012` |

¹ [`Keychron/qmk_firmware` → `keyboards/keychron/q1_he/{ansi,iso,jis}_encoder/info.json`](https://github.com/Keychron/qmk_firmware/tree/hall_effect_playground/keyboards/keychron/q1_he) (Hall-Effect boards live on the `hall_effect_playground` branch, not `master`; VID `0x3434`). The QMK tree names the board `q1_he` with no explicit "8K" variant — the Q1 HE 8K shares this `q1_he` namespace, confirmed empirically here since the factory-firmware PID `0x1012` drives the board on real hardware. Matching VIA definitions: [`SRGBmods/QMK-Binaries` → `VIA_JSON/keychron`](https://github.com/SRGBmods/QMK-Binaries/tree/main/VIA_JSON/keychron).
² The stock firmware shipped for the [Keychron Launcher](https://www.keychron.com/pages/firmware-and-json-files-of-the-keychron-he-series-keyboards); its PIDs aren't published in the open QMK / the-via repos. This preset's `0x1012` is a factory-firmware value — i.e. the JIS slot — matching the unit it was verified on.

## Docking-station gotcha

When the keyboard is reached through a **Link-KM docking station** (a separate accessory, not bundled with the board — this is the setup it was verified on), the dock **also enumerates as a `0x3434` raw-HID (`0xFF60`) device** (PID `0xd026`). The default `product_match` of `K8` matches neither the dock nor the keyboard, so detection can grab the dock — which answers neither the protocol probe nor value reads — and every command fails. The preset avoids this by matching `Q1 HE` (the dock reports `Keychron Link-KM`) and pinning `product_id` to the keyboard.

## Connection

Connect via USB cable (this board is wired for kbd-signal's purposes — raw HID runs over the cable). Do not run the VIA app / Keychron Launcher at the same time (concurrent raw HID writes race).
