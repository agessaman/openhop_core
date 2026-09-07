"""Tests for CompanionBridge enable/disable.

A disabled companion keeps its stores and preferences but leaves the mesh: it
claims no packet, ACKs nothing, queues nothing and transmits nothing. Settings
survive the round trip so re-enabling resumes on the same identity.
"""

import asyncio
import struct
from types import SimpleNamespace
from typing import Optional

import pytest

from openhop_core.companion import CompanionBridge
from openhop_core.companion.models import Contact
from openhop_core.protocol import LocalIdentity, Packet, PacketBuilder
from openhop_core.protocol.constants import PAYLOAD_TYPE_TXT_MSG, TXT_TYPE_PLAIN

# The ACK the text handler schedules fires at TXT_ACK_DELAY_MS (200ms); wait
# comfortably past it so "no ACK" means absent rather than merely not yet sent.
ACK_SETTLE_SEC = 0.5


class MockPacketInjector:
    """Records injected packets and returns True by default."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def __call__(
        self, pkt: Packet, wait_for_ack: bool = False, expected_crc: Optional[int] = None
    ) -> bool:
        self.calls.append((pkt, wait_for_ack))
        return True


def _make_bridge(enabled: bool = True):
    """Return a bridge plus its injector and a peer that is a known contact."""
    injector = MockPacketInjector()
    peer = LocalIdentity()
    bridge = CompanionBridge(LocalIdentity(), injector, node_name="Comp", enabled=enabled)
    bridge.contacts.add(Contact(public_key=peer.get_public_key(), name="Peer"))
    return bridge, injector, peer


def _make_dm(bridge, peer, text: bytes = b"ping", timestamp: int = 1_700_000_000) -> Packet:
    """Build a plain DM from ``peer`` to ``bridge`` — the type firmware ACKs."""
    flags = TXT_TYPE_PLAIN << 2
    plaintext = struct.pack("<I", timestamp) + bytes([flags]) + text
    recv_contact = SimpleNamespace(
        public_key=bridge.get_public_key().hex(), out_path=[], out_path_len=-1
    )
    payload, _, _ = PacketBuilder._create_encrypted_payload(recv_contact, peer, plaintext)
    pkt = Packet()
    pkt.header = PacketBuilder._create_header(PAYLOAD_TYPE_TXT_MSG, "direct", False)
    pkt.path_len, pkt.path = 0, bytearray()
    pkt.payload = bytearray(payload)
    pkt.payload_len = len(payload)
    return pkt


async def _settle() -> None:
    """Let the event service and the delayed-ACK task run to completion."""
    await asyncio.sleep(ACK_SETTLE_SEC)


class TestEnabledFlag:
    def test_enabled_by_default(self):
        bridge, _, _ = _make_bridge()
        assert bridge.enabled is True

    def test_constructor_can_start_disabled(self):
        bridge, _, _ = _make_bridge(enabled=False)
        assert bridge.enabled is False

    def test_enabled_is_independent_of_is_running(self):
        """A host may never call start(); disabled is policy, not lifecycle."""
        bridge, _, _ = _make_bridge()
        assert bridge.is_running is False
        assert bridge.enabled is True


@pytest.mark.asyncio
class TestDisabledCompanionIsOffTheAir:
    async def test_enabled_bridge_acks_and_queues(self):
        """Control case: the fixture really does produce an ACK and a queue entry."""
        bridge, injector, peer = _make_bridge()
        await bridge.process_received_packet(_make_dm(bridge, peer))
        await _settle()
        assert injector.calls, "enabled bridge should have ACKed the DM"
        assert bridge.message_queue.count == 1

    async def test_disabled_bridge_neither_acks_nor_queues(self):
        bridge, injector, peer = _make_bridge(enabled=False)
        result = await bridge.process_received_packet(_make_dm(bridge, peer))
        await _settle()
        assert injector.calls == []
        assert bridge.message_queue.count == 0
        assert result.authenticated is False

    async def test_disabled_bridge_leaves_packet_forwardable(self):
        """not_for_us keeps a dest-hash collision routable to the real owner.

        The one-byte hash a companion is keyed by can collide with a co-hosted
        identity, so a disabled bridge must not answer 'mine' — the host has to
        stay free to hand the packet on.
        """
        bridge, _, peer = _make_bridge(enabled=False)
        result = await bridge.process_received_packet(_make_dm(bridge, peer))
        assert result.authenticated is False
        assert result.response is None

    async def test_disabled_bridge_records_no_rx_stats(self):
        bridge, _, peer = _make_bridge(enabled=False)
        await bridge.process_received_packet(_make_dm(bridge, peer))
        assert bridge.stats.get_totals()["total_rx"] == 0

    async def test_disabled_bridge_refuses_to_transmit(self):
        bridge, injector, _ = _make_bridge(enabled=False)
        sent = await bridge._send_packet(Packet(), wait_for_ack=False)
        assert sent is False
        assert injector.calls == []

    async def test_disabled_bridge_ignores_flood_copies(self):
        """The return-path teacher gets no material while disabled."""
        bridge, _, _ = _make_bridge(enabled=False)
        teacher = bridge._protocol_response_handler.return_path_teacher
        noted = []
        teacher.note_flood_copy = lambda *args: noted.append(args)
        bridge.note_flood_copy(Packet(), b"", {})
        assert noted == []


@pytest.mark.asyncio
class TestSetEnabled:
    async def test_disabling_clears_the_queue(self):
        bridge, _, peer = _make_bridge()
        await bridge.process_received_packet(_make_dm(bridge, peer))
        await _settle()
        assert bridge.message_queue.count == 1

        await bridge.set_enabled(False)
        assert bridge.message_queue.count == 0

    async def test_disabling_cancels_an_ack_already_scheduled(self):
        """The ACK is scheduled on a 200ms delay, so a DM received just before the
        toggle would otherwise still ACK from a disabled companion."""
        bridge, injector, peer = _make_bridge()
        await bridge.process_received_packet(_make_dm(bridge, peer))
        await bridge.set_enabled(False)
        await _settle()
        assert injector.calls == []

    async def test_disabling_retains_settings_and_stores(self):
        bridge, _, peer = _make_bridge()
        bridge.set_channel(0, "Public", b"\x01" * 32)
        bridge.prefs.node_name = "KeepMe"

        await bridge.set_enabled(False)

        assert bridge.prefs.node_name == "KeepMe"
        assert bridge.contacts.get_count() == 1
        assert bridge.contacts.get_by_key(peer.get_public_key()) is not None
        assert bridge.get_channel(0) is not None

    async def test_re_enabling_resumes_delivery(self):
        bridge, injector, peer = _make_bridge()
        await bridge.set_enabled(False)
        await bridge.process_received_packet(_make_dm(bridge, peer, text=b"while-off"))
        await _settle()
        assert injector.calls == []

        await bridge.set_enabled(True)
        await bridge.process_received_packet(_make_dm(bridge, peer, text=b"after-on"))
        await _settle()
        assert injector.calls, "re-enabled bridge should ACK again"
        assert bridge.message_queue.count == 1

    async def test_enabling_an_enabled_bridge_keeps_its_queue(self):
        """A redundant toggle must not be a queue wipe: the daemon can replay the
        configured value on any settings save."""
        bridge, _, peer = _make_bridge()
        await bridge.process_received_packet(_make_dm(bridge, peer))
        await _settle()
        assert bridge.message_queue.count == 1

        await bridge.set_enabled(True)
        assert bridge.message_queue.count == 1

    async def test_set_enabled_coerces_truthy_values(self):
        bridge, _, _ = _make_bridge()
        await bridge.set_enabled(0)
        assert bridge.enabled is False
        await bridge.set_enabled(1)
        assert bridge.enabled is True
