# Keychron Q1 HE 8K

[English](keychron-q1-he-8k.md) | [日本語](keychron-q1-he-8k.ja.md)

Verified on real hardware. Preset: [`examples/config.q1-he-8k.json`](../../examples/config.q1-he-8k.json) — copy its `device` block into your `config.json`.

## Config

| Field | Value | Notes |
|-------|-------|-------|
| `vendor_id` | `0x3434` | Keychron |
| `product_id` | `0x1012` | the **wired keyboard** — see the docking-station note below |
| `product_match` | `Q1 HE` | |
| `v3_channel` | `3` | rgb_matrix custom channel |
| `effects` | `solid`=1, `breathing`=2 | same indices as the K8 Pro default |

- VIA protocol **13** (v3 custom channel). On channel 3 the value ids are `brightness`=1, `effect`=2, `speed`=3, `color`(hue, sat)=4.
- Hue uses the standard QMK 0–255 wheel (red=0, yellow≈43, green=85, blue=170), so kbd-signal's built-in colors need no remapping.

## Docking-station gotcha

The Q1 HE 8K comes with a **Link-KM docking station that also enumerates as a `0x3434` raw-HID (`0xFF60`) device** (PID `0xd026`; the wired keyboard is `0x1012`). The default `product_match` of `K8` matches neither, so detection can grab the docking station — which answers neither the protocol probe nor value reads — and every command fails. Pinning `product_id` to `0x1012` avoids it.

## Connection

Connect via USB cable with the rear switch on **"Cable"**. Do not run the VIA app / Keychron Launcher at the same time (concurrent raw HID writes race).
