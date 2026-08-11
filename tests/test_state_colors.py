"""Per-state colour from config (#49).

The three signals had their hue, saturation, speed and brightness written into
the code; only the effect indices came from config. A user whose board shows a
static colour could not tell the states apart, and #49 asked for exactly that:
red while the agent works, green when it finishes.

`states` in config.json now overrides any of those fields, and names its
animation through the device's `effects` map rather than by raw index, so a
states block stays valid on a board whose enabled-animation list differs.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from kbd_signal import config, states


class _ConfigFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "config.json")
        patch = mock.patch.object(config, "CONFIG_FILE", self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, cfg):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)


class DefaultsTests(_ConfigFile):
    def test_no_states_block_keeps_the_shipped_signals(self):
        """The whole feature has to be invisible to anyone who never asks."""
        self.write({})
        self.assertEqual(
            states.patterns(),
            {"waiting": {"effect": 2, "hue": 21, "sat": 255,
                         "speed": 170, "brightness": 255},
             "done": {"effect": 1, "hue": 85, "sat": 255, "brightness": 255},
             "error": {"effect": 2, "hue": 0, "sat": 255,
                       "speed": 255, "brightness": 255}})

    def test_done_still_carries_no_speed(self):
        """_looks_like_signal only compares keys a pattern specifies, and a
        solid effect has no speed to compare."""
        self.write({})
        self.assertNotIn("speed", states.patterns()["done"])


class NotPersistedTests(_ConfigFile):
    def test_setup_does_not_freeze_the_defaults_into_the_users_file(self):
        """`setup` writes back whatever config.load() hands it. If load()
        filled the defaults in, every config.json it touched would pin the
        colours of the version that ran it, and a later change to a default
        would silently never reach those users."""
        self.write({"restore": "off"})
        cfg = config.load()
        cfg["device"] = dict(config.DEFAULT_DEVICE)
        with mock.patch.object(config, "STATE_DIR", self.dir):
            config.save(cfg)
        with open(self.path, encoding="utf-8") as f:
            self.assertNotIn("states", json.load(f))


class OverrideTests(_ConfigFile):
    def test_one_field_overrides_and_the_rest_stay(self):
        self.write({"states": {"done": {"hue": 0}}})
        done = states.patterns()["done"]
        self.assertEqual(done["hue"], 0)
        self.assertEqual(done["sat"], 255)
        self.assertEqual(done["brightness"], 255)
        self.assertEqual(done["effect"], 1)

    def test_the_issue_49_case_red_while_working_green_when_done(self):
        self.write({"states": {"waiting": {"hue": 0, "effect": "solid"},
                               "done": {"hue": 85, "effect": "solid"}}})
        pats = states.patterns()
        self.assertEqual((pats["waiting"]["hue"], pats["waiting"]["effect"]),
                         (0, 1))
        self.assertEqual((pats["done"]["hue"], pats["done"]["effect"]), (85, 1))

    def test_an_effect_name_resolves_through_this_board_s_map(self):
        """The same states block on a board with a different animation list:
        the name is stable, the index is not."""
        self.write({"device": {"effects": {"solid": 7, "breathing": 8}},
                    "states": {"done": {"effect": "breathing"}}})
        self.assertEqual(states.patterns()["done"]["effect"], 8)

    def test_an_animation_the_defaults_never_named_can_be_used(self):
        """Adding one is adding a name to effects and using it here."""
        self.write({"device": {"effects": {"rainbow": 12}},
                    "states": {"error": {"effect": "rainbow"}}})
        self.assertEqual(states.patterns()["error"]["effect"], 12)


class RejectionTests(_ConfigFile):
    """Nothing here falls back quietly: this block is only ever hand-edited,
    and a typo that kept the old colour reads as "my config does nothing"."""

    def _reason(self, cfg):
        self.write(cfg)
        with self.assertRaises(ValueError) as caught:
            states.patterns()
        return str(caught.exception)

    def test_an_out_of_range_byte_is_named(self):
        self.assertIn("hue", self._reason({"states": {"done": {"hue": 300}}}))

    def test_a_non_integer_is_named(self):
        self.assertIn("brightness",
                      self._reason({"states": {"done": {"brightness": "max"}}}))

    def test_a_bool_is_not_an_integer_here(self):
        self.assertIn("sat", self._reason({"states": {"done": {"sat": True}}}))

    def test_an_unknown_effect_name_is_named_with_what_is_available(self):
        reason = self._reason({"states": {"done": {"effect": "sparkle"}}})
        self.assertIn("sparkle", reason)
        self.assertIn("solid", reason)

    def test_a_misspelled_state_is_rejected_rather_than_ignored(self):
        """Silently dropping it would look exactly like the setting not
        working."""
        reason = self._reason({"states": {"wating": {"hue": 0}}})
        self.assertIn("wating", reason)

    def test_a_states_block_of_the_wrong_shape_is_rejected(self):
        self.assertIn("states", self._reason({"states": [1, 2, 3]}))

    def test_a_falsy_state_override_is_rejected_not_silently_dropped(self):
        """`or {}` turned 0, "", [] and False into an empty override before
        the type check could see them, so the state quietly kept its
        defaults -- the "my config does nothing" failure this function exists
        to prevent. Only a truthy wrong type reached the check, which is why
        the sibling test above passed while these did not."""
        for falsy in (0, "", [], False):
            reason = self._reason({"states": {"done": falsy}})
            self.assertIn("done", reason, falsy)

    def test_a_falsy_states_block_is_rejected_too(self):
        for falsy in (0, "", [], False):
            self.assertIn("states", self._reason({"states": falsy}), falsy)

    def test_a_misspelled_field_is_rejected_rather_than_carried(self):
        """It would otherwise survive the merge and reach
        via.Keyboard.apply(**pattern) as an unexpected keyword -- raising only
        after the signal was recorded as active, so state.json would claim a
        light that was never written."""
        reason = self._reason({"states": {"done": {"brightnes": 42}}})
        self.assertIn("brightnes", reason)
        self.assertIn("brightness", reason)


class FieldsNotInAStatesDefaultsTests(_ConfigFile):
    def test_speed_can_be_added_to_done(self):
        """`done` ships without one because a solid effect has no speed, but
        switching it to a breathing effect and setting a speed is a thing to
        want. The check is against the fields a pattern may carry, not the
        ones this state happens to default."""
        self.write({"states": {"done": {"effect": "breathing", "speed": 90}}})
        done = states.patterns()["done"]
        self.assertEqual((done["effect"], done["speed"]), (2, 90))


class EndToEndTests(_ConfigFile):
    """The configured colour has to reach the keyboard, not just patterns()."""

    def setUp(self):
        super().setUp()
        for name, value in (("STATE_DIR", self.dir),
                            ("STATE_FILE", os.path.join(self.dir, "state.json")),
                            ("ACTIVE_FLAG", os.path.join(self.dir, "flag")),
                            ("LOG_FILE", os.path.join(self.dir, "log"))):
            patch = mock.patch.object(states, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_configured_hue_is_what_gets_written(self):
        self.write({"states": {"done": {"hue": 42, "brightness": 200}}})
        kb = mock.MagicMock()
        kb.__enter__ = mock.Mock(return_value=kb)
        kb.__exit__ = mock.Mock(return_value=False)
        kb.snapshot.return_value = {"effect": 16, "speed": 127,
                                    "brightness": 120, "color": [0, 255]}
        kb.apply.return_value = True
        with mock.patch.object(states.via, "Keyboard",
                               mock.Mock(return_value=kb)), \
             mock.patch.object(states, "_spawn_delayed_restore",
                               return_value=True):
            states.set_state("done")
        kb.apply.assert_called_once()
        self.assertEqual(kb.apply.call_args.kwargs["hue"], 42)
        self.assertEqual(kb.apply.call_args.kwargs["brightness"], 200)


if __name__ == "__main__":
    unittest.main()
