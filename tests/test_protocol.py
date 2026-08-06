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
        # max_brightness=None on purpose: this covers framing and the value-id
        # mapping, so the transport is idealised. Real v3 firmware scales
        # brightness and 120 would come back 119 (see BrightnessRoundTripTests);
        # folding that in here would make a framing regression read as an
        # arithmetic one.
        dev = fake_hid.FakeViaDevice(protocol=protocol, max_brightness=None)
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


class BrightnessRoundTripTests(unittest.TestCase):
    """VIA's brightness round-trip is lossy by construction.

    quantum/via.c stores scale8(value, RGB_MATRIX_MAXIMUM_BRIGHTNESS) -- an
    (i * sc) / 256 divide -- and reads back val * 255 / MAXIMUM_BRIGHTNESS. The
    two directions do not use the same divisor, so a written brightness does not
    come back. Modelling that is what makes #56 reproducible with no hardware:
    any code confirming a brightness write by comparing the read to what it wrote
    is wrong on VIA v3, and until now nothing in CI could say so.

    Deliberately separate from the fix (#57): these assert what the protocol
    does, not what kbd-signal decides about it, so they hold either way.
    """

    def test_a_written_brightness_reads_back_scaled(self):
        """assertNotEqual([120]) used to pass on a device that ignored the write
        entirely -- the untouched default 200 is also not 120. Assert the value
        the scaling actually produces."""
        dev = fake_hid.FakeViaDevice(protocol=13, max_brightness=255)
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                kb.set_value(via.VALUE_BRIGHTNESS, 120)
                self.assertEqual(kb.get_value(via.VALUE_BRIGHTNESS), [119])

    def test_the_loss_is_rounding_not_quantisation(self):
        """Distinct writes stay distinct on read-back, so this is two truncations
        rather than a coarse level scale -- matching the sweep measured on a
        Q1 HE 8K (65 distinct reads for 65 distinct writes, deltas 0 to -2)."""
        written = list(range(0, 256, 4))
        dev = fake_hid.FakeViaDevice(protocol=13, max_brightness=255)
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                reads = []
                for value in written:
                    kb.set_value(via.VALUE_BRIGHTNESS, value)
                    reads.append(kb.get_value(via.VALUE_BRIGHTNESS)[0])
        self.assertEqual(len(set(reads)), len(reads), "resolution was lost")
        self.assertTrue(all(0 <= w - r <= 2 for w, r in zip(written, reads)),
                        reads)

    def test_zero_round_trips_exactly(self):
        """Which is why this went unnoticed: a baseline of 0 with signals at full
        brightness only ever exercises the ends, where the round trip is exact."""
        dev = fake_hid.FakeViaDevice(protocol=13, max_brightness=255)
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                kb.set_value(via.VALUE_BRIGHTNESS, 0)
                self.assertEqual(kb.get_value(via.VALUE_BRIGHTNESS), [0])

    def test_v2_round_trips_exactly_so_a_read_back_check_is_sound_there(self):
        """v2 scales by RGBLIGHT_LIMIT_VAL / 255 both ways, lossless at QMK's
        default limit -- and v2 is where the #34 revert was measured, on a
        K8 Pro."""
        dev = fake_hid.FakeViaDevice(protocol=9)
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                kb.set_value(via.VALUE_BRIGHTNESS, 120)
                self.assertEqual(kb.get_value(via.VALUE_BRIGHTNESS), [120])
                self.assertTrue(kb.settle_brightness(120, settle=0, hold=0))


class SettleBrightnessTests(unittest.TestCase):
    """settle_brightness across the two protocol generations (#56), driven
    through the real byte path so the read-back the code sees comes from a model
    of the firmware rather than from a mock told what to return.

    Assertions are made on the packets the device received, which is the only
    way to say "no confirmation read happened" without trusting a clock.
    """

    @staticmethod
    def _brightness_gets(dev):
        """CUSTOM_GET packets addressed at the brightness value id."""
        wanted = [via.CMD_CUSTOM_GET, dev.channel,
                  via._VALUE_IDS[via.VALUE_BRIGHTNESS][1]]
        return [p for p in dev.packets if p[1:4] == wanted]

    @staticmethod
    def _brightness_sets(dev):
        wanted = [via.CMD_CUSTOM_SET, dev.channel,
                  via._VALUE_IDS[via.VALUE_BRIGHTNESS][1]]
        return [p for p in dev.packets if p[1:4] == wanted]

    def test_v3_succeeds_although_the_read_can_never_match(self):
        dev = fake_hid.FakeViaDevice(protocol=13, max_brightness=255)
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                self.assertTrue(kb.settle_brightness(120, settle=0, hold=0))
        self.assertNotEqual(dev.values[via.VALUE_BRIGHTNESS], 120,
                            "the model should be lossy, or this proves nothing")

    def test_v3_makes_no_confirmation_read(self):
        """The bug: because the read could never equal the write, the loop spun
        the full 1.50 s budget per restore with the state lock held and returned
        a False that was not a failure. Pinned on the wire, with no clock."""
        dev = fake_hid.FakeViaDevice(protocol=13, max_brightness=255)
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                dev.packets.clear()
                self.assertTrue(kb.settle_brightness(120, settle=0, hold=0))
        self.assertEqual(self._brightness_gets(dev), [],
                         "v3 must not attempt a confirmation read")

    def test_v3_still_rewrites_across_the_revert_window(self):
        """The rewrites are what #34 actually added; only the read-back went."""
        dev = fake_hid.FakeViaDevice(protocol=13, max_brightness=255)
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                dev.packets.clear()
                kb.settle_brightness(77, settle=0.01, hold=0.05)
        writes = self._brightness_sets(dev)
        self.assertGreater(len(writes), 1)
        self.assertEqual({p[4] for p in writes}, {77})

    def test_v2_still_confirms_by_read_back(self):
        """Backward compatibility: v2 scales by RGBLIGHT_LIMIT_VAL / 255 both
        ways, lossless at QMK's default limit, and v2 is where #34 was measured
        (a K8 Pro on stock protocol-9 firmware). The confirmation stays."""
        dev = fake_hid.FakeViaDevice(protocol=9)
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                dev.packets.clear()
                self.assertTrue(kb.settle_brightness(120, settle=0, hold=0))
        wanted = [via.CMD_CUSTOM_GET, via._VALUE_IDS[via.VALUE_BRIGHTNESS][0]]
        self.assertTrue([p for p in dev.packets if p[1:3] == wanted],
                        "v2 must still verify the write")
        self.assertEqual(dev.values[via.VALUE_BRIGHTNESS], 120)

    def test_write_errors_are_swallowed_so_a_hook_never_fails(self):
        dev = fake_hid.FakeViaDevice(protocol=13, max_brightness=255)
        with fake_hid.attached(dev):
            with _open(dev) as kb:
                dev.write = lambda _packet: (_ for _ in ()).throw(
                    OSError("device gone"))
                self.assertTrue(kb.settle_brightness(5, settle=0, hold=0))


class VerifyChannelTests(unittest.TestCase):
    """verify_channel against a board that answers a channel it does not drive.

    This is the case the simulator could not express before: `_decode` returning
    None modelled every wrong channel as silent, so the situation verify_channel
    exists for -- an answer that proves nothing -- was untestable, while #51
    measured it on real hardware (channel 0 answers with [0]; 5 and 7 do not).
    """

    @staticmethod
    def _cfg(channel):
        return {"vendor_id": 0x3434, "product_id": 0x1012,
                "product_match": "Fake", "v3_channel": channel,
                "reset_on_effect": False,
                "effects": {"solid": 1, "breathing": 2}}

    def test_an_echoing_wrong_channel_is_rejected(self):
        """A read-only probe would accept this board on any channel. The write
        round-trip is what tells them apart."""
        dev = fake_hid.FakeViaDevice(protocol=13, channel=3,
                                     echo_unknown_channel=True)
        with fake_hid.attached(dev):
            with via.Keyboard(self._cfg(6)) as kb:
                self.assertFalse(via.verify_channel(kb))

    def test_an_unowned_channel_answers_when_echoing(self):
        """The distinguishing behaviour, and the reason verify_channel writes.

        Asserting only that verify_channel rejects the channel is not enough:
        it rejects a silent one too, so that assertion holds with this whole
        mode removed. What must be pinned is that a *read* succeeds where it
        would otherwise raise -- that is what fools a read-only probe.
        """
        dev = fake_hid.FakeViaDevice(protocol=13, channel=3,
                                     echo_unknown_channel=True)
        with fake_hid.attached(dev):
            with via.Keyboard(self._cfg(6)) as kb:
                self.assertEqual(kb.get_value(via.VALUE_BRIGHTNESS, tries=2), [0])

    def test_a_write_to_an_unowned_channel_looks_like_it_worked(self):
        """set_value ignores a missing echo, so on an echoing board it returns
        normally while nothing lands -- the silent loss verify_channel exists to
        catch."""
        dev = fake_hid.FakeViaDevice(protocol=13, channel=3,
                                     echo_unknown_channel=True)
        with fake_hid.attached(dev):
            with via.Keyboard(self._cfg(6)) as kb:
                before = dict(dev.values)
                kb.set_value(via.VALUE_SPEED, 42)      # raises nothing
        self.assertEqual(dev.values, before)

    def test_the_driving_channel_is_confirmed(self):
        dev = fake_hid.FakeViaDevice(protocol=13, channel=3,
                                     echo_unknown_channel=True)
        with fake_hid.attached(dev):
            with via.Keyboard(self._cfg(3)) as kb:
                self.assertTrue(via.verify_channel(kb))

    def test_a_silent_wrong_channel_is_also_rejected(self):
        dev = fake_hid.FakeViaDevice(protocol=13, channel=3)
        with fake_hid.attached(dev):
            with via.Keyboard(self._cfg(6)) as kb:
                self.assertFalse(via.verify_channel(kb))

    def test_an_unknown_value_id_stays_silent_on_the_right_channel(self):
        """The flag models one measured behaviour -- an unowned *channel*
        answering -- and must not invent an answer for an id nobody measured."""
        for echo in (False, True):
            dev = fake_hid.FakeViaDevice(protocol=13, channel=3,
                                         echo_unknown_channel=echo)
            with fake_hid.attached(dev):
                with via.Keyboard(self._cfg(3)) as kb:
                    kb._drain()
                    kb._write(via.CMD_CUSTOM_GET, 3, 0x7E)  # no such value id
                    self.assertEqual(kb._dev.read(64, 250), [], f"echo={echo}")

    def test_v2_never_echoes_since_it_has_no_channel(self):
        dev = fake_hid.FakeViaDevice(protocol=9, echo_unknown_channel=True)
        with fake_hid.attached(dev):
            with via.Keyboard(self._cfg(3)) as kb:
                kb._drain()
                kb._write(via.CMD_CUSTOM_GET, 0x7E)
                self.assertEqual(kb._dev.read(64, 250), [])

    def test_the_probed_speed_is_restored_on_the_real_channel(self):
        """setup snapshots after this, so a leftover probe value would become
        the user's baseline."""
        dev = fake_hid.FakeViaDevice(protocol=13, channel=3,
                                     values={via.VALUE_SPEED: 170})
        with fake_hid.attached(dev):
            with via.Keyboard(self._cfg(3)) as kb:
                via.verify_channel(kb)
        self.assertEqual(dev.values[via.VALUE_SPEED], 170)

    def test_nothing_is_written_to_a_channel_the_board_does_not_own(self):
        """Writing to an unhandled channel is not inert on real firmware: #51
        left a board at effect 5 / brightness 255 and wedged the HID handle."""
        dev = fake_hid.FakeViaDevice(protocol=13, channel=3,
                                     echo_unknown_channel=True)
        with fake_hid.attached(dev):
            with via.Keyboard(self._cfg(6)) as kb:
                before = dict(dev.values)
                via.verify_channel(kb)
        self.assertEqual(dev.values, before)


if __name__ == "__main__":
    unittest.main()
