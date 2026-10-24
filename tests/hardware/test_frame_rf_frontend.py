"""Frame-protocol AGC/FEM/RX-boost client, with a scripted modem."""

import struct

from openhop_core.hardware.frame_rf_frontend import FrameRfControls
from openhop_core.hardware.kiss_modem_wrapper import KissModemWrapper
from openhop_core.hardware.protocol_constants import (
    AGC_INTERVAL_MAX_SEC,
    CMD_AGC_INTERVAL_RESP,
    CMD_ERROR,
    CMD_FEM_STATE_RESP,
    CMD_GET_AGC_INTERVAL,
    CMD_GET_RF_CAPS,
    CMD_RF_CAPS_RESP,
    CMD_RX_BOOST_RESP,
    CMD_SET_AGC_INTERVAL,
    CMD_SET_FEM_STATE,
    CMD_SET_RX_BOOST,
    ERR_RADIO_FAULT,
    ERR_UNSUPPORTED,
    FEM_STATE_RX_LNA,
    FEM_STATE_TX_PA,
    RF_CAP_AGC,
    RF_CAP_FEM_RX_LNA,
    RF_CAP_FEM_TX_PA,
    RF_CAP_RX_BOOSTED_GAIN,
)


def _u16(value: int) -> bytes:
    return struct.pack("<H", value)


def _u32(value: int) -> bytes:
    return struct.pack("<I", value)


class FakeModem(FrameRfControls):
    def __init__(self, radio_config=None):
        self._init_rf_controls(radio_config)
        self.sent = []
        self.script = []

    def _rf_transact_fresh(self, cmd, payload, expect, timeout):
        self.sent.append((cmd, bytes(payload), expect))
        self._rf_awaiting_reply = True
        if not self.script:
            return None
        reply = self.script.pop(0)
        if reply is None:
            return None
        self._rf_awaiting_reply = False
        return reply


def _caps(*bits: int) -> tuple[int, bytes]:
    mask = 0
    for bit in bits:
        mask |= bit
    return (CMD_RF_CAPS_RESP, _u32(mask))


def test_agc_interval_is_one_second_steps_up_to_an_hour():
    assert FrameRfControls.effective_agc_reset_interval(30) == 30
    assert FrameRfControls.effective_agc_reset_interval(1024) == 1024
    assert FrameRfControls.effective_agc_reset_interval(0) == 0
    assert FrameRfControls.effective_agc_reset_interval(AGC_INTERVAL_MAX_SEC) == 3600
    assert FrameRfControls.effective_agc_reset_interval(3601) is None
    assert FrameRfControls.effective_agc_reset_interval(True) is None

    # KISS firmware still rounds down to a multiple of 4 and stops at 1020.
    assert KissModemWrapper.effective_agc_reset_interval(30) == 28
    assert KissModemWrapper.effective_agc_reset_interval(10) == 8
    assert KissModemWrapper.effective_agc_reset_interval(1) == 0
    assert KissModemWrapper.effective_agc_reset_interval(1020) == 1020
    assert KissModemWrapper.effective_agc_reset_interval(1024) is None


def test_set_agc_sends_the_requested_second_and_keeps_the_reply():
    modem = FakeModem()
    modem.script.append((CMD_AGC_INTERVAL_RESP, _u16(30)))

    assert modem.set_agc_reset_interval(30) == 30
    assert modem.sent == [(CMD_SET_AGC_INTERVAL, _u16(30), CMD_AGC_INTERVAL_RESP)]
    assert modem.radio_config["agc_reset_interval_seconds"] == 30
    assert modem.applied_hardware_config["agc_reset_interval_seconds"] == 30


def test_agc_outside_range_is_not_sent():
    modem = FakeModem()
    assert modem.set_agc_reset_interval(3601) is None
    assert modem.set_agc_reset_interval(-1) is None
    assert modem.sent == []


def test_agc_error_does_not_update_saved_state():
    modem = FakeModem()
    modem.script.append((CMD_ERROR, bytes([0x06])))
    assert modem.set_agc_reset_interval(30) is None
    assert "agc_reset_interval_seconds" not in modem.radio_config


def test_capabilities_select_which_controls_answer():
    modem = FakeModem()
    modem.script.append(_caps(RF_CAP_AGC, RF_CAP_FEM_RX_LNA))

    assert modem.supports_agc_reset_control() is True
    assert modem.supports_fem_rx_gain() is True
    assert modem.supports_fem_tx_gain() is False
    assert modem.supports_rx_boosted_gain() is False
    assert modem.sent == [(CMD_GET_RF_CAPS, b"", CMD_RF_CAPS_RESP)]
    # The probe is cached.
    assert modem.supports_agc_reset_control() is True
    assert len(modem.sent) == 1


def test_fem_unsupported_bit_fails_the_whole_request():
    modem = FakeModem()
    modem.script.append((CMD_ERROR, bytes([ERR_UNSUPPORTED])))

    assert modem.set_fem_state(rx_gain=True, tx_gain=True) is None
    assert modem.set_fem_rx_gain(True) is False
    assert "fem_rx_gain" not in modem.radio_config
    assert "fem_tx_gain" not in modem.radio_config
    assert modem.sent[0] == (CMD_SET_FEM_STATE, bytes([0x03, 0x03]), CMD_FEM_STATE_RESP)


def test_fem_reply_is_remembered_only_for_bits_it_confirms():
    modem = FakeModem()
    modem.script.append((CMD_FEM_STATE_RESP, bytes([FEM_STATE_RX_LNA, FEM_STATE_RX_LNA])))

    state = modem.set_fem_state(rx_gain=True, tx_gain=False)
    assert state == {"rx_gain": True, "tx_gain": None}
    assert modem.radio_config["fem_rx_gain"] is True
    assert "fem_tx_gain" not in modem.radio_config


def test_rx_boost_fault_does_not_claim_the_requested_state():
    modem = FakeModem()
    modem.script.append((CMD_ERROR, bytes([ERR_RADIO_FAULT])))
    assert modem.set_rx_boosted_gain(True) is None
    assert "rx_boosted_gain" not in modem.radio_config

    modem.script.append((CMD_RX_BOOST_RESP, bytes([0x00])))
    assert modem.set_rx_boosted_gain(True) is False
    assert "rx_boosted_gain" not in modem.radio_config

    modem.script.append((CMD_RX_BOOST_RESP, bytes([0x01])))
    assert modem.set_rx_boosted_gain(True) is True
    assert modem.radio_config["rx_boosted_gain"] is True


def test_a_queued_reply_cannot_confirm_the_next_request():
    modem = FakeModem()
    modem._stale_rf.append((CMD_AGC_INTERVAL_RESP, _u16(4)))
    modem.script.append((CMD_AGC_INTERVAL_RESP, _u16(30)))

    assert modem.set_agc_reset_interval(30) == 30
    assert modem.script == []
    assert modem._stale_rf == []
    assert modem.sent == [(CMD_SET_AGC_INTERVAL, _u16(30), CMD_AGC_INTERVAL_RESP)]


def test_a_reply_after_timeout_is_discarded():
    modem = FakeModem()
    modem.script.append(None)
    assert modem.set_agc_reset_interval(30) is None
    assert CMD_AGC_INTERVAL_RESP in modem._rf_expect_stale

    with modem._response_lock:
        assert modem._rf_note_unsolicited(CMD_AGC_INTERVAL_RESP, _u16(30)) is True
    assert modem._stale_rf == []
    assert CMD_AGC_INTERVAL_RESP not in modem._rf_expect_stale

    modem.script.append((CMD_AGC_INTERVAL_RESP, _u16(12)))
    assert modem.set_agc_reset_interval(12) == 12


def test_saved_controls_are_sent_again_after_reconnect():
    modem = FakeModem(
        {
            "agc_reset_interval_seconds": 30,
            "fem_rx_gain": True,
            "rx_boosted_gain": False,
        }
    )
    caps = _caps(RF_CAP_AGC, RF_CAP_FEM_RX_LNA, RF_CAP_FEM_TX_PA, RF_CAP_RX_BOOSTED_GAIN)
    cycle = [
        caps,
        (CMD_AGC_INTERVAL_RESP, _u16(30)),
        (CMD_FEM_STATE_RESP, bytes([FEM_STATE_RX_LNA | FEM_STATE_TX_PA, FEM_STATE_RX_LNA])),
        (CMD_RX_BOOST_RESP, bytes([0x00])),
    ]
    modem.script.extend(cycle)
    modem.apply_saved_rf_controls()
    first = [item[0] for item in modem.sent]
    assert first == [
        CMD_GET_RF_CAPS,
        CMD_SET_AGC_INTERVAL,
        CMD_SET_FEM_STATE,
        CMD_SET_RX_BOOST,
    ]
    assert modem.applied_hardware_config["agc_reset_interval_seconds"] == 30
    assert modem.applied_hardware_config["fem_rx_gain"] is True
    assert modem.applied_hardware_config["rx_boosted_gain"] is False

    modem.script.extend(cycle)
    modem.apply_saved_rf_controls()
    assert modem.sent[len(first)][0] == CMD_GET_RF_CAPS
    assert modem.sent[len(first) + 1] == (CMD_SET_AGC_INTERVAL, _u16(30), CMD_AGC_INTERVAL_RESP)


def test_get_agc_reports_the_running_interval():
    modem = FakeModem()
    modem.script.append((CMD_AGC_INTERVAL_RESP, _u16(12)))
    assert modem.get_agc_reset_interval() == 12
    assert modem.sent == [(CMD_GET_AGC_INTERVAL, b"", CMD_AGC_INTERVAL_RESP)]
