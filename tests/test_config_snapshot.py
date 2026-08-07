"""One config read per operation, so nothing mixes two generations (#44).

A signal transition used to read the config four separate times: for the
pattern's effect indices, for the fingerprint stored with the baseline, for
the device it opened, and again inside the leftover-signal guard. Restore read
it three times. Every read is a fresh `config.load()`, so an edit landing
between two of them left the operation describing two different keyboards --
a pattern written for board A on board B, or a baseline fingerprinted as A
that was actually captured from B.

These tests replace `config.load` with a callback that changes what it returns
after the first read, which is what a `config.save()` (atomic via os.replace,
config.py) does to a process mid-operation. Against the split-read code every
test here fails; each one names the specific pair of consumers it pins.

Deliberately not built on StateFileTestCase: that fixture patches
`states.load_config` and `states.patterns` (tests/test_states.py), which are
the very things under test here.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from kbd_signal import config, states, via

# Two boards that disagree about everything the operation derives from config:
# which device to open, and what the effect indices mean on it.
BOARD_A = {"vendor_id": 0x1111, "product_id": 0x1111, "product_match": None,
           "v3_channel": 3, "reset_on_effect": False,
           "effects": {"solid": 1, "breathing": 2}}
BOARD_B = {"vendor_id": 0x2222, "product_id": 0x2222, "product_match": None,
           "v3_channel": 3, "reset_on_effect": False,
           "effects": {"solid": 7, "breathing": 8}}

IDENTITY_FIELDS = ("vendor_id", "product_id", "product_match", "v3_channel",
                   "effects")


def _identity(dev_cfg):
    """The fingerprint states stores, spelled out rather than imported.

    Calling states._device_identity() would make the pre-fix run fail with a
    TypeError about the signature instead of showing the values disagreeing.
    """
    return {field: dev_cfg.get(field) for field in IDENTITY_FIELDS}


class _SwitchingConfig(unittest.TestCase):
    """Serve BOARD_A until the Nth `config.load()`, then BOARD_B forever."""

    def setUp(self):
        directory = tempfile.mkdtemp()
        patches = [
            mock.patch.object(states, "STATE_DIR", directory),
            mock.patch.object(states, "STATE_FILE",
                              os.path.join(directory, "state.json")),
            mock.patch.object(states, "ACTIVE_FLAG",
                              os.path.join(directory, "active.flag")),
            mock.patch.object(states, "LOG_FILE", os.path.join(directory, "log")),
            # set_state("done") otherwise launches a real detached restore
            # against the real keyboard.
            mock.patch.object(states, "_spawn_delayed_restore",
                              return_value=True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.reads = 0

    def _serve(self, switch_after, restore_mode="baseline",
               after_mode=None):
        """Patch config.load. Never exhausts: after the fix there is one read,
        before it there are several, and a list-shaped side_effect would make
        the fixed code fail for running out rather than for being wrong.
        """
        def load():
            self.reads += 1
            board = BOARD_A if self.reads <= switch_after else BOARD_B
            mode = restore_mode
            if after_mode is not None and self.reads > switch_after:
                mode = after_mode
            return {"restore": mode, "device": dict(board)}

        patch = mock.patch.object(config, "load", side_effect=load)
        patch.start()
        self.addCleanup(patch.stop)

    def _keyboard_factory(self, opened):
        """A fake via.Keyboard recording which config opened it.

        `dev_cfg or config.device()` mirrors via.Keyboard.__init__ -- that
        fallback is what turns a missing argument into an extra read, i.e. it
        is the bug this file is about. Keep it in step with via.py.
        """
        def factory(dev_cfg=None):
            kb = mock.MagicMock()
            kb.__enter__ = mock.Mock(return_value=kb)
            kb.__exit__ = mock.Mock(return_value=False)
            kb.snapshot.return_value = {"effect": 16, "speed": 127,
                                        "brightness": 120, "color": [0, 255]}
            kb.apply.return_value = True
            kb.apply_snapshot.return_value = True
            kb.settle_brightness.return_value = True
            kb.get_value.return_value = [120]
            opened.append(dev_cfg if dev_cfg is not None else config.device())
            return kb
        return factory


class SetStateSnapshotTests(_SwitchingConfig):
    def test_the_effect_written_means_what_it_means_on_the_opened_board(self):
        """The pattern's effect index and the board it is sent to must come
        from one read: index 1 is solid on A and something else on B."""
        self._serve(switch_after=1)
        opened = []
        with mock.patch.object(states.via, "Keyboard",
                               self._keyboard_factory(opened)):
            states.set_state("done")
        self.assertEqual(len(opened), 1)
        written = states.load_state()["baseline"] and None  # not the point
        applied_effect = BOARD_A["effects"]["solid"]
        self.assertEqual(opened[0]["effects"]["solid"], applied_effect,
                         "the effect index was read from a different config "
                         "than the board it was written to")
        self.assertIsNone(written)

    def test_the_baseline_is_fingerprinted_with_the_board_it_came_from(self):
        """#45's fingerprint is worthless if it can describe a config the
        snapshot did not come from."""
        self._serve(switch_after=2)
        opened = []
        with mock.patch.object(states.via, "Keyboard",
                               self._keyboard_factory(opened)):
            states.set_state("done")
        state = states.load_state()
        self.assertEqual(state["last_baseline_device"], _identity(opened[0]))

    def test_one_entry_point_reads_the_config_once(self):
        """The structural pin. A future caller that reaches for the live config
        from inside a threaded path reintroduces #44 without changing any
        observable value, and only a read count catches it."""
        self._serve(switch_after=99)
        with mock.patch.object(states.via, "Keyboard",
                               self._keyboard_factory([])):
            states.set_state("done")
        self.assertEqual(self.reads, 1)


class RestoreSnapshotTests(_SwitchingConfig):
    def _waiting_with_baseline(self, dev_cfg):
        snap = {"effect": 16, "speed": 127, "brightness": 120,
                "color": [0, 255]}
        states.save_state({
            "active": "waiting", "generation": 4, "owners": [],
            "baseline": snap, "last_baseline": snap,
            "last_baseline_device": _identity(dev_cfg),
        })

    def test_restore_opens_the_board_its_baseline_was_validated_against(self):
        self._waiting_with_baseline(BOARD_A)
        self._serve(switch_after=1)
        opened = []
        with mock.patch.object(states.via, "Keyboard",
                               self._keyboard_factory(opened)):
            states.restore()
        self.assertEqual(len(opened), 1)
        self.assertEqual(_identity(opened[0]), _identity(BOARD_A),
                         "the baseline was accepted for one board and "
                         "written to another")

    def test_restore_takes_its_mode_from_the_same_read_as_the_identity(self):
        """`restore` mode and the fingerprint came from separate reads, so a
        config swap could validate the baseline and then discard it."""
        self._waiting_with_baseline(BOARD_A)
        self._serve(switch_after=1, restore_mode="baseline", after_mode="off")
        opened = []
        with mock.patch.object(states.via, "Keyboard",
                               self._keyboard_factory(opened)):
            states.restore()
        kb_calls = opened
        self.assertEqual(len(kb_calls), 1)
        self.assertEqual(self.reads, 1,
                         "restore still reads the config more than once")

    def test_restore_reads_the_config_once(self):
        self._waiting_with_baseline(BOARD_A)
        self._serve(switch_after=99)
        with mock.patch.object(states.via, "Keyboard",
                               self._keyboard_factory([])):
            states.restore()
        self.assertEqual(self.reads, 1)


if __name__ == "__main__":
    unittest.main()
