"""Emit a device-preset skeleton for `docs/devices/` and `examples/`.

A working config only helps the person who has it. This turns one into the two
files a device page is made of, so a "it works on my board" report can become a
pull request.

What it fills in is only the mechanical half: the config table, the VIA protocol
number, and the `detect --all` output. The parts that make an existing page
worth reading -- PID schemes cross-checked against public QMK/VIA definitions,
docking-station traps, which firmware generation a value came from -- are marked
TODO, because generating them would produce a page that looks researched and
isn't.

Read-only: the device is opened for the protocol probe and nothing is written,
so unlike `setup` this is safe to run while a signal is showing.
"""

import json
import re
import sys

from . import config, via

_PLACEHOLDER = "TODO"


def slug(product_string):
    """`Keychron Q1 HE 8K` -> `keychron-q1-he-8k`, for use in file names."""
    text = re.sub(r"[^a-z0-9]+", "-", (product_string or "").lower())
    return text.strip("-") or "unknown-device"


def _config_json(dev_cfg, product_string):
    """The examples/config.<slug>.json body: the device block plus the
    description field the existing presets carry."""
    device = {k: dev_cfg[k] for k in ("vendor_id", "product_id",
                                      "product_match", "v3_channel",
                                      "reset_on_effect", "effects")}
    for key in ("vendor_id", "product_id"):
        if isinstance(device[key], int):
            device[key] = f"{device[key]:#06x}"
    return {
        "description": (
            f"kbd-signal device preset for the {product_string or _PLACEHOLDER}. "
            f"{_PLACEHOLDER}: say how this was verified, whether product_id "
            "varies by layout, and anything that competes for the same VID. "
            "Copy the device block into your config.json."),
        "device": device,
    }


def _device_page(dev_cfg, product_string, protocol, found, name):
    """The docs/devices/<slug>.md skeleton."""
    v3 = protocol >= 11
    rows = [
        ("`vendor_id`", f"`{dev_cfg['vendor_id']:#06x}`", _PLACEHOLDER),
        ("`product_id`",
         "`null`" if dev_cfg["product_id"] is None
         else f"`{dev_cfg['product_id']:#06x}`",
         "this unit's PID — say whether it varies by layout"),
        ("`product_match`",
         "`null`" if not dev_cfg.get("product_match")
         else f"`{dev_cfg['product_match']}`",
         _PLACEHOLDER),
        ("`v3_channel`", f"`{dev_cfg['v3_channel']}`",
         "rgb_matrix custom channel" if v3 else "unused on a v2 board"),
        ("`reset_on_effect`", f"`{str(dev_cfg['reset_on_effect']).lower()}`",
         "measured by `kbd-signal setup`; see below"),
        ("`effects`",
         ", ".join(f"`{k}`={v}" for k, v in sorted(dev_cfg["effects"].items())),
         _PLACEHOLDER),
    ]
    table = "\n".join(f"| {f} | {v} | {n} |" for f, v, n in rows)
    listing = "\n".join(
        f"found: {d.get('manufacturer_string')} {d.get('product_string')} "
        f"(VID={d['vendor_id']:#06x} PID={d['product_id']:#06x})" for d in found)
    quirk = (
        "resets color to red and brightness to full ~50–150 ms **after** an "
        "effect change, so kbd-signal holds the LEDs dark across that window "
        "and settles the color afterward"
        if dev_cfg["reset_on_effect"] else
        "does not show the post-effect reset, so colors are written directly "
        "with no dark hold")
    return f"""# {product_string or _PLACEHOLDER}

[English]({name}.md) | [日本語]({name}.ja.md)

{_PLACEHOLDER}: state how far this was verified (real hardware? which OS? which
signals round-tripped?). Preset: [`examples/config.{name}.json`](../../examples/config.{name}.json) — copy its `device` block into your `config.json`.

## Config

| Field | Value | Notes |
|-------|-------|-------|
{table}

- VIA protocol **{protocol}** ({'v3 custom channel' if v3 else 'v2 lighting'}). \
{f"On channel {dev_cfg['v3_channel']} the value ids are `brightness`=1, `effect`=2, `speed`=3, `color`(hue, sat)=4."
 if v3 else "Lighting ids are `brightness`=0x80, `effect`=0x81, `speed`=0x82, `color`=0x83."}
- `reset_on_effect` is `{str(dev_cfg['reset_on_effect']).lower()}` because this board {quirk}.
- {_PLACEHOLDER}: confirm the hue wheel matches QMK's 0–255 convention \
(red=0, green=85, blue=170). If it does not, kbd-signal's built-in colors need remapping.
- {_PLACEHOLDER}: confirm which direction `speed` runs. Some firmware treats 0 \
as fastest, which would make `waiting` look static and `error` look slow.

## Detection

`kbd-signal detect --all` on the machine this was verified on:

```
{listing}
```

{_PLACEHOLDER}: if anything else shares this VID (a dock, a receiver, a second
board), say so here and explain why `product_match` / `product_id` picks the
right one. `find_device_path` falls back to the first enumerated device when
nothing matches the product string, so an unpinned `product_id` can grab the
wrong device.

{_PLACEHOLDER}: cross-check the PID against public definitions if they exist
(`Keychron/qmk_firmware`, `SRGBmods/QMK-Binaries` → `VIA_JSON`, or the vendor's
own JSON) and note which firmware generation this value comes from.

## Connection

{_PLACEHOLDER}: how the board must be attached (raw HID needs a wired USB path;
some boards have a Cable/Wireless switch). Do not run the VIA app / vendor
configurator at the same time — concurrent raw HID writes race.
"""


def run():
    """Print the two files' contents. Returns a process exit code."""
    dev_cfg = config.device()
    found = via.enumerate_raw_hid(dev_cfg["vendor_id"])
    dev = via.find_device(dev_cfg)  # raises DeviceNotFound; cli.main reports it
    product_string = dev.get("product_string")
    with via.Keyboard(dev_cfg) as kb:
        protocol = kb.protocol
    name = slug(product_string)

    print(f"===== examples/config.{name}.json =====")
    print(json.dumps(_config_json(dev_cfg, product_string), indent=2))
    print(f"\n===== docs/devices/{name}.md =====")
    print(_device_page(dev_cfg, product_string, protocol, found, name))
    # The reminder goes to stderr so `kbd-signal export > page.md` keeps it out
    # of the file; flush first or the redirected stdout lands after it.
    sys.stdout.flush()
    print(f"Fill in every {_PLACEHOLDER} before opening a pull request, and "
          f"translate the page to docs/devices/{name}.ja.md.", file=sys.stderr)
    return 0
