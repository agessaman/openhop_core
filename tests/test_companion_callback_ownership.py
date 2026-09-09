"""Shared bridge subscribers survive frame-server reconnects without duplication."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openhop_core.companion.base_callbacks import _CallbackMixin
from openhop_core.companion.base_support import PUSH_CALLBACK_KEYS
from openhop_core.companion.frame_server import CompanionFrameServer


class Bridge(_CallbackMixin):
    def __init__(self):
        self._push_callbacks = {key: [] for key in PUSH_CALLBACK_KEYS}


class Subscriber:
    def __init__(self):
        self.calls = []

    async def receive(self, *args):
        self.calls.append(args)


@pytest.mark.asyncio
@pytest.mark.parametrize("event_name", PUSH_CALLBACK_KEYS)
async def test_registration_is_idempotent_per_owner_and_full_clear_releases_it(event_name):
    bridge = Bridge()
    first, second = Subscriber(), Subscriber()
    register = getattr(bridge, "on_" + event_name)
    for _ in range(3):
        register(first.receive)
        register(second.receive)
    await bridge._fire_callbacks(event_name, "payload")
    assert first.calls == [("payload",)]
    assert second.calls == [("payload",)]

    bridge.clear_push_callbacks()
    await bridge._fire_callbacks(event_name, "cleared")
    assert first.calls == [("payload",)]
    register(first.receive)
    await bridge._fire_callbacks(event_name, "restored")
    assert first.calls == [("payload",), ("restored",)]
    assert second.calls == [("payload",)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy,event_name,fields",
    [
        (
            "message_received",
            "message_event",
            "sender_key text timestamp txt_type packet_hash snr rssi "
            "sender_prefix path_len queued",
        ),
        (
            "channel_message_received",
            "channel_message_event",
            "channel_name sender_name text timestamp path_len channel_idx "
            "packet_hash snr rssi queued",
        ),
        (
            "channel_data_received",
            "channel_data_event",
            "channel_idx path_len data_type payload packet_hash snr rssi queued",
        ),
    ],
)
async def test_legacy_registration_preserves_arguments_and_full_clear(legacy, event_name, fields):
    bridge = Bridge()
    first, second = Subscriber(), Subscriber()
    register = getattr(bridge, "on_" + legacy)
    event = SimpleNamespace(**{name: index for index, name in enumerate(fields.split())})
    expected = tuple(range(len(fields.split())))
    direct = Mock()
    getattr(bridge, "on_" + event_name)(direct)
    for _ in range(3):
        register(first.receive)
        register(second.receive)
    await bridge._fire_callbacks(event_name, event)
    direct.assert_called_once_with(event)
    assert first.calls == [expected]
    assert second.calls == [expected]

    bridge.clear_push_callbacks()
    register(first.receive)
    await bridge._fire_callbacks(event_name, event)
    assert first.calls == [expected, expected]
    assert second.calls == [expected]
    direct.assert_called_once_with(event)


@pytest.mark.asyncio
async def test_reconnect_keeps_persistence_and_other_servers_registered_once():
    bridge = Bridge()
    first = CompanionFrameServer(bridge, "one", port=0)
    second = CompanionFrameServer(bridge, "two", port=0)
    first._enqueue_frame = Mock()
    second._enqueue_frame = Mock()
    persistence = Subscriber()
    bridge.on_message_event(persistence.receive)
    # Repeater.start registers its persistence callbacks before any TCP client.
    bridge.on_message_event(first._on_message_event)
    for _ in range(3):
        first._setup_push_callbacks()
        second._setup_push_callbacks()
    event = SimpleNamespace(
        sender_key=b"key",
        txt_type=0,
        timestamp=1,
        text="hello",
        path_len=0,
        packet_hash="hash",
        snr=0,
        rssi=0,
        sender_prefix=b"",
        queued=False,
    )
    await bridge._fire_callbacks("message_event", event)
    assert persistence.calls == [(event,)]
    first._enqueue_frame.assert_called_once()
    second._enqueue_frame.assert_called_once()
