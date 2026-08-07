"""Interactive first-run device config (`kbd-signal setup`).

Of the six keys in a `device` config block, only the two effect indices cannot
be detected -- the firmware's enabled-animation list is not readable over raw
HID -- so this module detects what it can (device choice, VIA v3 custom
channel, reset_on_effect) and asks for those two by eye.

Lives outside cli.py for the same reason kbd_signal.hooks does: cli.py holds
argument parsing and thin dispatch, and a command that needs helpers of its own
gets a module named after its domain.
"""

import json
import sys

from . import config, states, via

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


def run():
    """Walk the setup flow. Returns a process exit code."""
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

    # v3_channel comes from config: it is the one value setup cannot work out,
    # and verify_channel's failure tells the user to set it there and re-run --
    # which only helps if the re-run actually reads it. Everything else is either
    # the device just chosen or something setup determines below.
    dev_cfg = {**config.DEFAULT_DEVICE,
               "vendor_id": dev["vendor_id"],
               "product_id": dev["product_id"],
               "product_match": dev.get("product_string") or None,
               "v3_channel": config.device()["v3_channel"]}
    with via.Keyboard(dev_cfg) as kb:
        channel = dev_cfg["v3_channel"]
        if kb.protocol >= 11:
            if not via.verify_channel(kb):
                print(f"kbd-signal: custom channel {channel} does not drive "
                      "this board — a write did not read back.\n"
                      "Set \"v3_channel\" in config.json to the board's "
                      "id_qmk_rgb_matrix_channel (from its VIA definition) and "
                      "run setup again. Guessing it here would mean writing to "
                      "channels that mean something else on your firmware.",
                      file=sys.stderr)
                return 1
            print(f"VIA protocol {kb.protocol} (v3), custom channel {channel} "
                  "(write confirmed)")
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
    # A second read, minutes after the one at the top, so edits to keys outside
    # "device" survive the interview. The device block itself is replaced
    # wholesale on the next line -- that is what this command is for -- so an
    # edit to any of its fields is discarded, v3_channel included: the block
    # writes the channel verify_channel actually confirmed, not whatever the
    # file says now. What remains is the ordinary read-modify-write gap between
    # this line and the save -- an edit landing inside it is overwritten rather
    # than merged. #44 closed the split reads within one operation; this gap is
    # milliseconds and nobody has hit it.
    cfg = config.load()  # keeps "restore" and any other keys
    cfg["device"] = device
    config.save(cfg)
    print(f"wrote {config.CONFIG_FILE}")
    print("run `kbd-signal test` to check the three states.")
    return 0
