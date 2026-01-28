"""Comprehensive tests for KLS button window extraction.

These tests verify the CORRECT interpretation of the 8-digit button window
within the 24-digit KLS string.

The button window is at 0-indexed positions 9-16 (1-indexed positions 10-17).
For button N (1-8), read from index = 9 + (N-1).

Digit semantics:
    1 = ON (relay closed)
    2 = OFF (relay open)
    0, 3 = OFF (unknown/flash)
"""

import pytest
from datetime import datetime, timedelta

from models import (
    KLSState,
    CCOAddress,
    CCODevice,
    CCOEntityType,
    CCO_BUTTON_WINDOW_OFFSET,
)
from pyhomeworks import KLSMessage, MessageParser


class TestButtonWindowExtraction:
    """Tests for extracting the 8-digit button window from KLS."""

    def test_window_offset_is_9(self):
        """Verify the button window starts at index 9."""
        assert CCO_BUTTON_WINDOW_OFFSET == 9

    def test_window_indices(self):
        """Verify the button window is at indices 9-16."""
        kls_string = "000000000222112110000000"
        window = kls_string[9:17]
        assert window == "22211211"
        assert len(window) == 8

    def test_button_6_index_calculation(self):
        """Verify button 6 is at index 14."""
        index = CCO_BUTTON_WINDOW_OFFSET + (6 - 1)
        assert index == 14


class TestSampleKLSLines:
    """Test with the exact sample KLS lines from the protocol."""

    def test_sample_1_button_6_off(self):
        """
        KLS, [02:06:03], 000000000222112110000000
        Button 6 = index 14 = digit '2' = OFF
        """
        kls_string = "000000000222112110000000"
        led_states = [int(c) for c in kls_string]
        kls = KLSState(address="[02:06:03]", led_states=led_states)

        # Verify raw digit
        assert led_states[14] == 2

        # Verify interpreted state
        assert kls.get_cco_state(6) is False

    def test_sample_2_button_6_on(self):
        """
        KLS, [02:06:03], 000000000222111110000000
        Button 6 = index 14 = digit '1' = ON
        """
        kls_string = "000000000222111110000000"
        led_states = [int(c) for c in kls_string]
        kls = KLSState(address="[02:06:03]", led_states=led_states)

        # Verify raw digit
        assert led_states[14] == 1

        # Verify interpreted state
        assert kls.get_cco_state(6) is True

    def test_sample_1_all_buttons(self):
        """
        Window: 22211211
        Button states: OFF, OFF, OFF, ON, ON, OFF, ON, ON
        """
        kls_string = "000000000222112110000000"
        led_states = [int(c) for c in kls_string]
        kls = KLSState(address="[02:06:03]", led_states=led_states)

        expected = {
            1: False,  # index 9, digit 2
            2: False,  # index 10, digit 2
            3: False,  # index 11, digit 2
            4: True,   # index 12, digit 1
            5: True,   # index 13, digit 1
            6: False,  # index 14, digit 2
            7: True,   # index 15, digit 1
            8: True,   # index 16, digit 1
        }

        for button, expected_state in expected.items():
            actual = kls.get_cco_state(button)
            assert actual == expected_state, \
                f"Button {button}: expected {expected_state}, got {actual}"

    def test_sample_2_all_buttons(self):
        """
        Window: 22211111
        Button states: OFF, OFF, OFF, ON, ON, ON, ON, ON
        """
        kls_string = "000000000222111110000000"
        led_states = [int(c) for c in kls_string]
        kls = KLSState(address="[02:06:03]", led_states=led_states)

        expected = {
            1: False, 2: False, 3: False, 4: True,
            5: True, 6: True, 7: True, 8: True,
        }

        for button, expected_state in expected.items():
            assert kls.get_cco_state(button) == expected_state


class TestMessageParserKLS:
    """Test KLS parsing through MessageParser."""

    def test_parse_sample_1(self):
        parser = MessageParser()
        data = b"KLS, [02:06:03], 000000000222112110000000\r\n"
        messages = parser.feed(data)

        assert len(messages) == 1
        msg = messages[0]
        assert isinstance(msg, KLSMessage)
        assert msg.get_cco_relay_state(6) is False

    def test_parse_sample_2(self):
        parser = MessageParser()
        data = b"KLS, [02:06:03], 000000000222111110000000\r\n"
        messages = parser.feed(data)

        msg = messages[0]
        assert msg.get_cco_relay_state(6) is True


class TestCCODeviceInterpretation:
    """Test CCODevice state interpretation with button window."""

    def test_normal_device_sample_1(self):
        device = CCODevice(
            address=CCOAddress(2, 6, 3, 6),
            name="Test",
            entity_type=CCOEntityType.SWITCH,
            inverted=False,
        )

        # Sample 1: button 6 digit = 2
        assert device.interpret_state(2) is False

    def test_normal_device_sample_2(self):
        device = CCODevice(
            address=CCOAddress(2, 6, 3, 6),
            name="Test",
            entity_type=CCOEntityType.SWITCH,
            inverted=False,
        )

        # Sample 2: button 6 digit = 1
        assert device.interpret_state(1) is True

    def test_inverted_device_sample_1(self):
        device = CCODevice(
            address=CCOAddress(2, 6, 3, 6),
            name="Inverted",
            entity_type=CCOEntityType.SWITCH,
            inverted=True,
        )

        # Sample 1: button 6 digit = 2, inverted = ON
        assert device.interpret_state(2) is True

    def test_inverted_device_sample_2(self):
        device = CCODevice(
            address=CCOAddress(2, 6, 3, 6),
            name="Inverted",
            entity_type=CCOEntityType.SWITCH,
            inverted=True,
        )

        # Sample 2: button 6 digit = 1, inverted = OFF
        assert device.interpret_state(1) is False


class TestPartialFrames:
    """Test partial/combined RS232 frame handling."""

    def test_fragmented_kls(self):
        parser = MessageParser()

        # Send in fragments
        assert len(parser.feed(b"KLS, [02:06")) == 0
        assert len(parser.feed(b":03], 000000")) == 0
        messages = parser.feed(b"000222112110000000\r\n")

        assert len(messages) == 1
        assert messages[0].get_cco_relay_state(6) is False

    def test_combined_messages(self):
        parser = MessageParser()
        data = (
            b"KLS, [02:06:03], 000000000222112110000000\r\n"
            b"KLS, [02:06:03], 000000000222111110000000\r\n"
        )
        messages = parser.feed(data)

        assert len(messages) == 2
        assert messages[0].get_cco_relay_state(6) is False
        assert messages[1].get_cco_relay_state(6) is True


class TestEdgeCases:
    """Edge case tests."""

    def test_button_0_returns_false(self):
        kls = KLSState(address="[02:06:03]", led_states=[1] * 24)
        assert kls.get_cco_state(0) is False

    def test_button_9_returns_false(self):
        kls = KLSState(address="[02:06:03]", led_states=[1] * 24)
        assert kls.get_cco_state(9) is False

    def test_all_zeros_means_off(self):
        kls = KLSState(address="[02:06:03]", led_states=[0] * 24)
        for button in range(1, 9):
            assert kls.get_cco_state(button) is False

    def test_window_all_ones_means_on(self):
        led_states = [0] * 9 + [1] * 8 + [0] * 7
        kls = KLSState(address="[02:06:03]", led_states=led_states)
        for button in range(1, 9):
            assert kls.get_cco_state(button) is True

    def test_digit_3_flash2_means_off(self):
        led_states = [0] * 9 + [3] * 8 + [0] * 7
        kls = KLSState(address="[02:06:03]", led_states=led_states)
        for button in range(1, 9):
            assert kls.get_cco_state(button) is False

    def test_stale_state_detection(self):
        kls = KLSState(
            address="[02:06:03]",
            led_states=[0] * 24,
            timestamp=datetime.now() - timedelta(minutes=5),
        )
        age = datetime.now() - kls.timestamp
        assert age > timedelta(minutes=1)
