"""Regression vectors checked against MeshCore upstream/dev 65aa1138."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openhop_core.companion.base_send import _SendOpsMixin
from openhop_core.companion.constants import PUSH_CODE_TELEMETRY_RESPONSE
from openhop_core.companion.frame_server import CompanionFrameServer
from openhop_core.protocol import CryptoUtils, Identity, LocalIdentity, PacketBuilder
from openhop_core.protocol.cayenne_lpp import encode_gps
from openhop_core.protocol.constants import (
    ADVERT_FLAG_HAS_NAME,
    PAYLOAD_TYPE_GRP_DATA,
    PAYLOAD_TYPE_REQ,
)


@pytest.mark.parametrize("kind,limit", [("normal", 167), ("anon", 136), ("group", 168)])
def test_firmware_builder_preflight(kind, limit, monkeypatch):
    local = LocalIdentity()
    dest = Identity(LocalIdentity().get_public_key())
    secret = bytes(range(32))

    def build(size):
        data = b"x" * size
        if kind == "normal":
            return PacketBuilder.create_datagram(PAYLOAD_TYPE_REQ, dest, local, secret, data)
        if kind == "anon":
            return PacketBuilder.create_anon_req(dest, local, secret, data)
        return PacketBuilder.create_group_data_packet(
            PAYLOAD_TYPE_GRP_DATA, 1, secret, data, secret
        )

    assert build(limit).payload_len <= 184
    encrypt = Mock(side_effect=AssertionError("must reject before encryption"))
    monkeypatch.setattr(PacketBuilder, "_encrypt_payload", encrypt)
    with pytest.raises(ValueError, match="firmware"):
        build(limit + 1)
    encrypt.assert_not_called()


@pytest.mark.parametrize("anonymous,limit", [(False, 162), (True, 132)])
def test_contact_builders_cannot_bypass_limits(anonymous, limit):
    local = LocalIdentity()
    contact = SimpleNamespace(public_key=LocalIdentity().get_public_key().hex(), out_path_len=0)
    if anonymous:

        def build(n):
            return PacketBuilder.create_anon_request(contact, local, b"x" * n, timestamp=1)

    else:

        def build(n):
            return PacketBuilder.create_protocol_request(
                contact, local, PAYLOAD_TYPE_REQ, b"x" * n, timestamp=1
            )

    assert build(limit)[0].payload_len <= 184
    with pytest.raises(ValueError, match="firmware"):
        build(limit + 1)


@pytest.mark.parametrize("character", ["é", "€", "😀"])
def test_advert_preserves_only_complete_utf8(character):
    for cut in range(1, len(character.encode())):
        prefix = "x" * (31 - cut)
        data = PacketBuilder._encode_advert_data(prefix + character)
        assert data == bytes([ADVERT_FLAG_HAS_NAME]) + prefix.encode()
    assert PacketBuilder._encode_advert_data("x" * 31)[1:] == b"x" * 31


def test_advert_name_flag_and_nul():
    assert PacketBuilder._encode_advert_data(None) == b"\x00"
    assert PacketBuilder._encode_advert_data("", flags=ADVERT_FLAG_HAS_NAME) == b"\x00"
    assert PacketBuilder._encode_advert_data("\x00hidden") == b"\x00"
    assert PacketBuilder._encode_advert_data("name\x00hidden")[1:] == b"name"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,data,expected",
    [
        (1, b"", "0967c57e3ed413b29584cd510327c3f0cd74"),
        (3, b"\x00", "8996922bfe6492f22ba3665e39ae892c2064"),
        (3, b"\x04", None),
    ],
)
async def test_fixed_request_envelope_rebuilt_for_retry(monkeypatch, code, data, expected):
    secret = bytes(range(32))
    local = LocalIdentity()
    proxy = SimpleNamespace(public_key=LocalIdentity().get_public_key().hex(), out_path_len=0)
    bridge = _SendOpsMixin()
    bridge._identity = local
    bridge.contacts = SimpleNamespace(get_by_key=lambda _: proxy, get_proxy_by_key=lambda _: proxy)
    bridge._get_protocol_response_handler = lambda: Mock()
    bridge._wait_for_path_propagation = AsyncMock()
    packets = []

    async def start(build, *args, **kwargs):
        packets.extend([build()[0], build()[0]])
        return {"success": False}

    bridge._start_request = start
    monkeypatch.setattr(PacketBuilder, "_get_timestamp", lambda: 0x01020304)
    monkeypatch.setattr(
        PacketBuilder, "_calc_shared_secret_and_key", lambda *args: (secret, secret[:16])
    )
    monkeypatch.setattr(
        "openhop_core.companion.base_send.os.urandom",
        Mock(side_effect=[bytes.fromhex("d4c3b2a1"), b"abcd"]),
    )
    await bridge._start_protocol_request(b"key", code, data, timeout=1, log_label="test")
    for packet, tail in zip(packets, [bytes.fromhex("d4c3b2a1"), b"abcd"]):
        plaintext = CryptoUtils.mac_then_decrypt(secret[:16], secret, bytes(packet.payload[2:]))
        assert (
            plaintext
            == b"\x04\x03\x02\x01" + bytes([code]) + data.ljust(4, b"\x00") + tail + b"\x00" * 3
        )
    if expected:
        assert bytes(packets[0].payload[2:]).hex() == expected


def test_generic_binary_request_body_is_unchanged(monkeypatch):
    local = LocalIdentity()
    contact = SimpleNamespace(public_key=LocalIdentity().get_public_key().hex(), out_path_len=0)
    secret = bytes(range(32))
    monkeypatch.setattr(
        PacketBuilder, "_calc_shared_secret_and_key", lambda *args: (secret, secret[:16])
    )
    packet, _ = PacketBuilder.create_protocol_request(contact, local, 3, b"raw", timestamp=1)
    assert CryptoUtils.mac_then_decrypt(
        secret[:16], secret, bytes(packet.payload[2:])
    ) == b"\x01\x00\x00\x00\x03raw" + bytes(8)


@pytest.mark.parametrize("temperature", [None, float("nan"), float("inf"), 25.0])
def test_self_telemetry_mcu_slot_order(temperature):
    bridge = Mock()
    bridge.get_public_key.return_value = bytes(range(32))
    server = CompanionFrameServer(bridge, "hash", port=0)
    server._get_batt_and_storage = lambda: (4200, 0, 0)
    server._get_mcu_temperature_c = lambda: temperature
    server._get_self_telemetry_lpp = lambda: bytes.fromhex("026700c8")
    frames = []
    server._write_frame = frames.append
    server._push_self_telemetry()
    mcu = "016700fa" if temperature == 25.0 else ""
    assert frames == [
        bytes([PUSH_CODE_TELEMETRY_RESPONSE, 0])
        + bytes(range(6))
        + bytes.fromhex("017401a3" + mcu + "026700c8")
    ]


def test_gps_matches_signed_24_bit_firmware_vector():
    assert encode_gps(1, 1.25, -2.5, -3.75).hex() == "01880030d4ff9e58fffe89"


def test_truncated_advert_signature_covers_canonical_appdata():
    local = LocalIdentity()
    packet = PacketBuilder.create_advert(local, "x" * 30 + "é", flags=1)
    payload = bytes(packet.payload)
    assert payload[100:] == bytes([ADVERT_FLAG_HAS_NAME | 1]) + b"x" * 30
    assert Identity(local.get_public_key()).verify(payload[:36] + payload[100:], payload[36:100])
