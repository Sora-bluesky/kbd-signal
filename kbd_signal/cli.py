"""kbd-signal CLI entry point."""

import argparse
import json
import sys
import time

from . import states, via


def cmd_detect(args):
    from . import config
    if getattr(args, "all", False):
        found = via.enumerate_raw_hid(None)
        if not found:
            print("No raw HID (0xFF60) interfaces found on any device.")
            return 1
        for d in found:
            print(f"found: {d.get('manufacturer_string')} {d.get('product_string')} "
                  f"(VID={d['vendor_id']:#06x} PID={d['product_id']:#06x})")
        print('\nPut vendor_id/product_id into config.json under "device" '
              "to target one of these.")
        return 0
    found = via.enumerate_raw_hid(config.device()["vendor_id"])
    if not found:
        print("Configured keyboard's raw HID interface not found. "
              "Connect via USB cable (rear switch on Cable). "
              "Use `kbd-signal detect --all` to list every raw-HID device.")
        return 1
    for d in found:
        print(f"found: {d.get('product_string')} "
              f"(VID={d['vendor_id']:#06x} PID={d['product_id']:#06x})")
    with via.Keyboard() as kb:
        proto = kb.protocol
        snap = kb.snapshot()
    print(f"VIA protocol: {proto} ({'v3 custom channel' if proto >= 11 else 'v2 lighting'})")
    print(f"current: effect={snap['effect']} speed={snap['speed']} "
          f"brightness={snap['brightness']} hue,sat={snap['color']}")
    return 0


# QMK's default RGB_MATRIX order puts SOLID_COLOR at 1 and BREATHING at 2, so
# the interview below normally ends after two questions. 0 is often "all off"
# (useless as a signal), hence last of the low indices.
EFFECT_CANDIDATES = (1, 2, 0) + tuple(range(3, 41))

# Visible probe pattern for the interview: bright enough to judge, slow enough
# that a breathing effect reads as a pulse.
_INTERVIEW = dict(hue=via.PROBE_HUE, sat=255, speed=170, brightness=200)


def _ask(prompt, choices):
    """Prompt until the answer is one of `choices`. None on EOF (piped stdin)."""
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            return None
        if answer in choices:
            return answer
        print(f"  answer one of: {', '.join(c for c in choices if c)}")


def _choose_device(found):
    if len(found) == 1:
        return found[0]
    print("raw-HID devices:")
    for i, d in enumerate(found, 1):
        print(f"  [{i}] {d.get('product_string') or '?'} "
              f"(VID={d['vendor_id']:#06x} PID={d['product_id']:#06x})")
    answer = _ask(f"which one? [1-{len(found)}]: ",
                  tuple(str(i) for i in range(1, len(found) + 1)))
    return None if answer is None else found[int(answer) - 1]


def _interview(kb):
    """Ask which effect index looks steady and which pulses.

    The firmware's enabled-animation list is not exposed over raw HID, so these
    two indices cannot be detected -- only recognised by eye. The questions
    avoid QMK vocabulary ("solid", "breathing") because users of other boards
    do not know their firmware's animation names.
    """
    print("\nWatch the keyboard and say how each effect looks.")
    found = {}
    for n in EFFECT_CANDIDATES:
        if len(found) == 2:
            break
        kb.apply(effect=n, **_INTERVIEW)
        answer = _ask(f"  effect={n}: [s] steady / [p] pulsing / [n] neither: ",
                      ("s", "p", "n"))
        if answer is None:
            return None
        if answer == "s":
            found.setdefault("solid", n)
        elif answer == "p":
            found.setdefault("breathing", n)
    if len(found) < 2:
        print("  no steady and pulsing pair found in effects 0-40.",
              file=sys.stderr)
        return None
    return found


def cmd_setup(_args):
    from . import config
    # A signal on screen would be snapshotted as "the user's lighting" and
    # written back at the end -- the same baseline pollution as #32.
    #
    # ponytail: this guard catches a signal that is *already* showing, not a
    # hook that fires *during* the interview -- that hook's raw-HID writes race
    # the probes and can mislead an answer. Holding the state lock for the whole
    # interview would stall every hook for its 3 s timeout (minutes of them), so
    # the guard stays advisory: setup is a first-run command. Take the lock (or
    # add a "setup in progress" flag the hooks check) only if the race is
    # actually reported.
    if states.is_active():
        print("kbd-signal: a signal is showing right now; run setup when idle "
              "(the current lighting would be captured as your baseline).",
              file=sys.stderr)
        return 1
    found = via.enumerate_raw_hid(None)
    if not found:
        print("kbd-signal: no raw HID (0xFF60) interfaces found. Connect the "
              "keyboard by USB cable (rear switch on Cable).", file=sys.stderr)
        return 1
    dev = _choose_device(found)
    if dev is None:
        return 1

    dev_cfg = {**config.DEFAULT_DEVICE,
               "vendor_id": dev["vendor_id"],
               "product_id": dev["product_id"],
               "product_match": dev.get("product_string") or None}
    with via.Keyboard(dev_cfg) as kb:
        channel = dev_cfg["v3_channel"]
        if kb.protocol >= 11:
            channel = via.probe_channel(kb)
            if channel is None:
                print("kbd-signal: no VIA v3 custom channel answered; this "
                      "board may not expose rgb_matrix.", file=sys.stderr)
                return 1
            print(f"VIA protocol {kb.protocol} (v3), custom channel {channel}")
        else:
            print(f"VIA protocol {kb.protocol} (v2 lighting)")

        snap = kb.snapshot()
        print(f"saved current lighting: effect={snap['effect']} "
              f"speed={snap['speed']} brightness={snap['brightness']} "
              f"hue,sat={snap['color']}")
        try:
            # Any index other than the current one; the quirk needs a change.
            quirk = via.probe_reset_on_effect(
                kb, 2 if snap["effect"] == 1 else 1)
            kb._reset_on_effect = quirk  # interview uses the real write path
            print("reset-after-effect quirk: "
                  f"{'detected' if quirk else 'not detected'}")
            effects = _interview(kb)
        except KeyboardInterrupt:
            effects = None
        finally:
            kb.apply_snapshot(snap)

    if effects is None:
        print("\naborted; lighting restored, config unchanged.", file=sys.stderr)
        return 1

    # product_match is written even when null: omitting it lets load() merge the
    # "K8" default back in and mis-target a non-Keychron board.
    device = {
        "vendor_id": f"{dev['vendor_id']:#06x}",
        "product_id": f"{dev['product_id']:#06x}",
        "product_match": dev.get("product_string") or None,
        "v3_channel": channel,
        "reset_on_effect": quirk,
        "effects": effects,
    }
    print("\nwriting:")
    for key, value in device.items():
        print(f"  {key:<15} {json.dumps(value)}")
    if _ask(f"write to {config.CONFIG_FILE}? "
            "(previous kept as .bak) [y/N]: ", ("y", "n", "")) != "y":
        print("not written; lighting restored.", file=sys.stderr)
        return 1
    cfg = config.load()  # keeps "restore" and any other keys
    cfg["device"] = device
    config.save(cfg)
    print(f"wrote {config.CONFIG_FILE}")
    print("run `kbd-signal test` to check the three states.")
    return 0


def cmd_set(args):
    ok = states.set_state(args.state)
    if not ok and sys.stdout.isatty():
        print("keyboard unavailable (see log)", file=sys.stderr)
    return 0


def cmd_restore(args):
    states.restore(after=args.after, generation=args.gen)
    return 0


def cmd_test(_args):
    for name in ("waiting", "done", "error"):
        print(f"-> {name}")
        if not states.set_state(name):
            print("keyboard unavailable", file=sys.stderr)
            return 1
        time.sleep(3)
    print("-> restore")
    states.restore()
    return 0


def cmd_raw_effect(args):
    with via.Keyboard() as kb:
        kb.set_value(via.VALUE_EFFECT, args.n)
    print(f"effect set to {args.n}")
    return 0


def cmd_hook(args):
    from . import hooks
    try:
        if args.source == "claude":
            hooks.handle_claude()
        else:
            hooks.handle_codex(args.rest)
    except Exception as e:  # never fail the calling agent
        states.log(f"hook {args.source} error: {e!r}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="kbd-signal",
                                description="Agent status -> VIA keyboard backlight")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="interactive first-run config for a new keyboard"
                   ).set_defaults(fn=cmd_setup)

    sp = sub.add_parser("detect", help="list device and current lighting")
    sp.add_argument("--all", action="store_true",
                    help="list every raw-HID (0xFF60) device regardless of "
                         "the configured vendor id")
    sp.set_defaults(fn=cmd_detect)

    sp = sub.add_parser("set", help="enter a signal state")
    sp.add_argument("state", choices=sorted(states.STATE_NAMES))
    sp.set_defaults(fn=cmd_set)

    sp = sub.add_parser("restore", help="restore baseline lighting")
    sp.add_argument("--after", type=float, default=None)
    sp.add_argument("--gen", type=int, default=None)
    sp.set_defaults(fn=cmd_restore)

    sub.add_parser("test", help="play all patterns then restore").set_defaults(fn=cmd_test)

    sp = sub.add_parser("raw-effect", help="set a raw effect index (debug)")
    sp.add_argument("n", type=int)
    sp.set_defaults(fn=cmd_raw_effect)

    sp = sub.add_parser("hook", help="entry point for agent hooks")
    sp.add_argument("source", choices=["claude", "codex"])
    sp.add_argument("rest", nargs=argparse.REMAINDER)
    sp.set_defaults(fn=cmd_hook)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (via.DeviceNotFound, OSError) as e:
        # Diagnostic commands report a missing or unopenable keyboard cleanly
        # with exit 1 (OSError covers open_path/read/write failures, e.g. the
        # device disappearing between enumeration and open).
        # Hook paths never raise (cmd_hook catches everything itself).
        print(f"kbd-signal: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
