"""kbd-signal CLI entry point."""

import argparse
import sys
import time

from . import states, via


def cmd_detect(_args):
    import hid
    found = [d for d in hid.enumerate(via.VENDOR_ID)
             if d.get("usage_page") == via.USAGE_PAGE
             and d.get("usage") == via.USAGE]
    if not found:
        print("Keychron raw HID interface not found. "
              "Connect via USB cable (Cable mode, or BT mode with cable attached).")
        return 1
    for d in found:
        print(f"found: {d.get('product_string')} "
              f"(VID={d['vendor_id']:#06x} PID={d['product_id']:#06x})")
    with via.Keyboard() as kb:
        snap = kb.snapshot()
    print(f"current: effect={snap['effect']} speed={snap['speed']} "
          f"brightness={snap['brightness']} hue,sat={snap['color']}")
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
                                description="Agent status -> Keychron K8 Pro backlight")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("detect", help="list device and current lighting").set_defaults(fn=cmd_detect)

    sp = sub.add_parser("set", help="enter a signal state")
    sp.add_argument("state", choices=sorted(states.PATTERNS))
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
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
