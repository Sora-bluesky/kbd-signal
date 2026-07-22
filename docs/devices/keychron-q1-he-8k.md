# Keychron Q1 HE 8K

[English](keychron-q1-he-8k.md) | [日本語](keychron-q1-he-8k.ja.md)

Verified on real hardware. Preset: [`examples/config.q1-he-8k.json`](../../examples/config.q1-he-8k.json) — copy its `device` block into your `config.json`.

## Config

| Field | Value | Notes |
|-------|-------|-------|
| `vendor_id` | `0x3434` | Keychron |
| `product_id` | `0x1012` | this unit's PID — **may differ on your board**; see the note below |
| `product_match` | `Q1 HE` | tells the keyboard apart from the Link-KM dock |
| `v3_channel` | `3` | rgb_matrix custom channel |
| `effects` | `solid`=1, `breathing`=2 | same indices as the K8 Pro default |

- VIA protocol **13** (v3 custom channel). On channel 3 the value ids are `brightness`=1, `effect`=2, `speed`=3, `color`(hue, sat)=4.
- Hue uses the standard QMK 0–255 wheel (red=0, yellow≈43, green=85, blue=170), so kbd-signal's built-in colors need no remapping.

### About `product_id` (0x1012)

`0x1012` is what **this unit** reports — verified with `kbd-signal detect --all`:

```
found: Keychron  Keychron Link-KM (VID=0x3434 PID=0xd026)
found: Keychron Keychron Q1 HE 8K (VID=0x3434 PID=0x1012)
```

Keychron assigns different PIDs per physical layout (ANSI / ISO / JIS), so your board may report a different value. Run `kbd-signal detect --all` and pin the PID it prints for your keyboard. Because `product_match: "Q1 HE"` already distinguishes the keyboard from the dock (see below), you can also drop `product_id` and rely on `product_match` alone.

## Docking-station gotcha

When the keyboard is reached through a **Link-KM docking station** (a separate accessory, not bundled with the board — this is the setup it was verified on), the dock **also enumerates as a `0x3434` raw-HID (`0xFF60`) device** (PID `0xd026`). The default `product_match` of `K8` matches neither the dock nor the keyboard, so detection can grab the dock — which answers neither the protocol probe nor value reads — and every command fails. The preset avoids this by matching `Q1 HE` (the dock reports `Keychron Link-KM`) and pinning `product_id` to the keyboard.

## Connection

Connect via USB cable with the rear switch on **"Cable"**. Do not run the VIA app / Keychron Launcher at the same time (concurrent raw HID writes race).
