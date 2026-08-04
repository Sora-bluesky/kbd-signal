"""The VIA protocol layer, driven end to end through a byte-level fake device.

These are the only tests that exercise via.Keyboard's actual wire behaviour: the
report framing, the v2/v3 command shape, the value-id mapping, the protocol
probe, the read offsets, and the echo draining. Everything else in the suite
mocks set_value/get_value and therefore assumes all of that is correct.
"""

import unittest

import fake_hid
from kbd_signal import via

CFG = {"vendor_id": 0x3434, "product_id": 0x1012, "product_match": "Fake",
       "v3_channel": 3, "reset_on_effect": False,
       "effects": {"solid": 1, "breathing": 2}}


def _open(device):
    return via.Keyboard(CFG)


class ProtocolProbeTests(unittest.TestCase):
    def test_v3_protocol_is_parsed_from_two_bytes(self):
        dev = fake_hid.FakeViaDevice(protocol=13)
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                self.assertEqual(kb.protocol, 13)
                self.assertTrue(kb._v3)

    def test_v2_protocol_is_parsed_and_selects_v2_framing(self):
        dev = fake_hid.FakeViaDevice(protocol=9)
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                self.assertEqual(kb.protocol, 9)
                self.assertFalse(kb._v3)

    def test_high_byte_is_not_dropped(self):
        """(resp[1] << 8) | resp[2] -- a board reporting 0x010B must not read
        as 11."""
        dev = fake_hid.FakeViaDevice(protocol=0x010B)
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                self.assertEqual(kb.protocol, 0x010B)

    def test_a_silent_device_raises_rather_than_guessing(self):
        dev = fake_hid.FakeViaDevice(protocol=13)
        dev._dispatch = lambda payload: None  # answers nothing
        with fake_hid.attached(dev):
            with self.assertRaises(IOError):
                _open(dev)

    def test_the_device_is_opened_by_the_enumerated_path(self):
        dev = fake_hid.FakeViaDevice()
        with fake_hid.attached(dev):
            with _open(dev):
                pass
        self.assertEqual(dev.opened, fake_hid.DEFAULT_ENTRY["path"])
        self.assertTrue(dev.closed)


class FramingTests(unittest.TestCase):
    def test_every_report_is_report_id_plus_report_size(self):
        dev = fake_hid.FakeViaDevice()
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                kb.set_value(via.VALUE_BRIGHTNESS, 120)
                kb.get_value(via.VALUE_BRIGHTNESS)
        self.assertTrue(dev.packets)
        for packet in dev.packets:
            self.assertEqual(len(packet), 1 + via.REPORT_SIZE)
            self.assertEqual(packet[0], 0x00)

    def test_v3_set_puts_the_channel_before_the_value_id(self):
        dev = fake_hid.FakeViaDevice(protocol=13, channel=3)
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                dev.packets.clear()
                kb.set_value(via.VALUE_EFFECT, 2)
        self.assertEqual(dev.packets[0][1:5],
                         [via.CMD_CUSTOM_SET, 3, 2, 2])

    def test_v2_set_has_no_channel_byte_and_uses_the_0x8x_ids(self):
        dev = fake_hid.FakeViaDevice(protocol=9)
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                dev.packets.clear()
                kb.set_value(via.VALUE_EFFECT, 2)
        self.assertEqual(dev.packets[0][1:4], [via.CMD_CUSTOM_SET, 0x81, 2])

    def test_color_carries_hue_and_sat(self):
        dev = fake_hid.FakeViaDevice()
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                dev.packets.clear()
                kb.set_value(via.VALUE_COLOR, 85, 254)
        self.assertEqual(dev.packets[0][1:6],
                         [via.CMD_CUSTOM_SET, 3, 4, 85, 254])

    def test_a_get_on_another_channel_gets_no_answer(self):
        """The board answers only its own channel, so a wrong v3_channel must
        fail loudly instead of silently mis-driving the keyboard."""
        dev = fake_hid.FakeViaDevice(protocol=13, channel=3)
        with fake_hid.attached(dev):
            with via.Keyboard({**CFG, "v3_channel": 5}) as kb:
                with self.assertRaises(IOError):
                    kb.get_value(via.VALUE_BRIGHTNESS, tries=1)


class ValueRoundTripTests(unittest.TestCase):
    def _round_trip(self, protocol):
        dev = fake_hid.FakeViaDevice(protocol=protocol)
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                kb.set_value(via.VALUE_BRIGHTNESS, 120)
                kb.set_value(via.VALUE_EFFECT, 2)
                kb.set_value(via.VALUE_SPEED, 170)
                kb.set_value(via.VALUE_COLOR, 85, 255)
                return kb.snapshot()

    def test_v3_round_trip(self):
        self.assertEqual(self._round_trip(13),
                         {"brightness": 120, "effect": 2, "speed": 170,
                          "color": [85, 255]})

    def test_v2_round_trip(self):
        self.assertEqual(self._round_trip(9),
                         {"brightness": 120, "effect": 2, "speed": 170,
                          "color": [85, 255]})

    def test_reads_land_on_the_right_offset(self):
        """v3 responses carry channel+id before the data, v2 only the id. A
        wrong offset would return the id byte as the value."""
        for protocol in (9, 13):
            dev = fake_hid.FakeViaDevice(protocol=protocol)
            with fake_hid.attached(dev), _open(dev) as kb:
                kb.set_value(via.VALUE_BRIGHTNESS, 7)
                self.assertEqual(kb.get_value(via.VALUE_BRIGHTNESS), [7],
                                 f"protocol {protocol}")

    def test_stale_echoes_do_not_answer_a_later_read(self):
        """_drain clears the queue before each request; a leftover SET echo
        must not be mistaken for the GET's response."""
        dev = fake_hid.FakeViaDevice()
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                kb.set_value(via.VALUE_SPEED, 99)
                dev._queue.append([via.CMD_CUSTOM_SET, 3, 1, 42])  # stray echo
                self.assertEqual(kb.get_value(via.VALUE_SPEED), [99])


class ApplyTests(unittest.TestCase):
    def test_plain_board_writes_directly_in_order(self):
        dev = fake_hid.FakeViaDevice()
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                dev.packets.clear()
                self.assertTrue(kb.apply(effect=2, hue=85, sat=255, speed=170,
                                         brightness=200))
        ids = [p[3] for p in dev.packets]  # v3 value id
        self.assertEqual(ids, [2, 3, 4, 1])  # effect, speed, color, brightness
        self.assertEqual(dev.values[via.VALUE_COLOR], [85, 255])
        self.assertEqual(dev.values[via.VALUE_BRIGHTNESS], 200)

    def test_quirk_board_drops_dark_before_changing_the_effect(self):
        """On a reset-prone board the reset must never land while the LEDs are
        bright, so brightness=0 precedes the effect write (#25)."""
        dev = fake_hid.FakeViaDevice(quirk_after=2)
        with fake_hid.attached(dev):
            with via.Keyboard({**CFG, "reset_on_effect": True}) as kb:
                dev.packets.clear()
                ok = kb.apply(effect=2, hue=85, sat=255, brightness=200,
                              )
        self.assertTrue(ok)
        first_two = [(p[3], p[4]) for p in dev.packets[:2]]
        self.assertEqual(first_two[0], (1, 0))   # brightness = 0
        self.assertEqual(first_two[1][0], 2)     # then effect

    def test_quirk_board_settles_the_color_through_the_reset(self):
        """The reset clobbers the color written in the same cycle; set_color has
        to notice via read-back and rewrite. Exercised here through the real
        byte path rather than a mocked set_value."""
        dev = fake_hid.FakeViaDevice(quirk_after=2)
        with fake_hid.attached(dev):
            with via.Keyboard({**CFG, "reset_on_effect": True}) as kb:
                kb.set_value(via.VALUE_EFFECT, 2)   # arms the reset
                self.assertTrue(kb.set_color(85, 255, hold=0, settle=0))
        self.assertEqual(dev.values[via.VALUE_COLOR], [85, 255])

    def test_apply_snapshot_restores_every_field(self):
        dev = fake_hid.FakeViaDevice()
        snap = {"brightness": 33, "effect": 7, "speed": 44, "color": [21, 255]}
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                self.assertTrue(kb.apply_snapshot(snap))
                self.assertEqual(kb.snapshot(), snap)


class SettleBrightnessTests(unittest.TestCase):
    def test_confirms_a_brightness_that_sticks(self):
        dev = fake_hid.FakeViaDevice()
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                self.assertTrue(kb.settle_brightness(77, hold=0, settle=0))
        self.assertEqual(dev.values[via.VALUE_BRIGHTNESS], 77)


if __name__ == "__main__":
    unittest.main()
