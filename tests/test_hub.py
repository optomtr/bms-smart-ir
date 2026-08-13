"""The hub, driven through a real socket to a real protocol implementation.

These are the tests that would have caught the failures the current release
has: several sessions to one device, a burst that outruns the emitter, a lost
command reported as delivered, a single timeout taking the session down.
"""

from __future__ import annotations

import asyncio

import pytest
from broadlink_sim import Farm

from custom_components.bms_smart_ir import const
from custom_components.bms_smart_ir.hub import (
    STATUS_ONLINE,
    STATUS_RECONNECTING,
    BroadlinkHub,
)

IR = bytes.fromhex("260048004e1a1e1a1e341e1a1e000d05")


def frame(index: int) -> bytes:
    """A distinguishable IR frame, so order and losses are visible."""
    return IR + bytes([index])


@pytest.fixture(autouse=True)
def _real_sockets(real_sockets):
    """Every test in this file drives the simulator through a real socket."""


@pytest.fixture
def farm():
    with Farm(2, base_port=22000, models=["rm4pro", "rm4mini"]) as running:
        yield running


async def make_hub(hass, farm: Farm, index: int = 0, *, heartbeat: bool = False):
    host, port = farm.address(index)
    hub = BroadlinkHub(hass, host, port=port, timeout=1)
    hub.gap_ms = 20  # the bench does not need the real 180 ms
    hub.sensor_interval = 3600
    await hub.async_start(with_heartbeat=heartbeat)
    return hub


async def test_one_device_gets_one_session(hass, farm: Farm):
    """The whole point of the hub: a climate, a TV and a poll share a session."""
    hub = await make_hub(hass, farm)
    try:
        results = await asyncio.gather(*(hub.async_send(frame(i)) for i in range(3)))
        assert results == [True, True, True]
        assert farm.devices[0].stats.auths == 1
    finally:
        await hub.async_stop()


async def test_burst_of_thirty_arrives_complete_and_spaced(hass, farm: Farm):
    """30 button presses at once: none lost, none reordered, none overlapping."""
    hub = await make_hub(hass, farm)
    try:
        results = await asyncio.gather(*(hub.async_send(frame(i)) for i in range(30)))
        assert all(results)

        device = farm.devices[0]
        assert [event.data for event in device.ir_log] == [frame(i) for i in range(30)]
        gaps = device.ir_gaps_ms()
        assert min(gaps) >= hub.gap_ms * 0.8, f"emitter got no pause: {min(gaps):.1f}ms"
    finally:
        await hub.async_stop()


async def test_state_changes_collapse_into_one_transmission(hass, farm: Farm):
    """Dragging a thermostat sends the final state once, not every step."""
    hub = await make_hub(hass, farm)
    try:
        results = await asyncio.gather(
            *(hub.async_send(frame(i), coalesce_key="ac") for i in range(5))
        )
        assert all(results)
        assert [event.data for event in farm.devices[0].ir_log] == [frame(4)]
        assert hub.stats.coalesced == 4
    finally:
        await hub.async_stop()


async def test_button_presses_are_never_collapsed(hass, farm: Farm):
    """Two presses of 'volume up' must be two transmissions."""
    hub = await make_hub(hass, farm)
    try:
        await asyncio.gather(hub.async_send(IR), hub.async_send(IR))
        assert len(farm.devices[0].ir_log) == 2
    finally:
        await hub.async_stop()


async def test_a_lost_command_is_reported_as_lost(hass, farm: Farm):
    hub = await make_hub(hass, farm)
    try:
        await hub.async_send(IR)
        farm.devices[0].faults.offline = True

        assert await hub.async_send(IR) is False
        assert hub.stats.failed == 1
    finally:
        await hub.async_stop()


async def test_one_timeout_does_not_drop_the_session(hass, farm: Farm):
    """A single Wi-Fi hiccup must not cost a handshake — that took floors down."""
    device = farm.devices[0]
    hub = await make_hub(hass, farm)
    try:
        assert await hub.async_send(IR) is True
        device.faults.drop_rate = 1.0
        assert await hub.async_send(IR) is False
        device.faults.drop_rate = 0.0
        assert await hub.async_send(IR) is True

        assert device.stats.auths == 1
        assert hub.status == STATUS_ONLINE
    finally:
        await hub.async_stop()


async def test_repeated_timeouts_do_drop_the_session(hass, farm: Farm):
    device = farm.devices[0]
    hub = await make_hub(hass, farm)
    try:
        await hub.async_send(IR)
        device.faults.offline = True
        for _ in range(const.FAILURE_THRESHOLD):
            await hub.async_send(IR)

        assert hub.status == STATUS_RECONNECTING
        assert hub.available is True, "grace period must hide a short outage"
    finally:
        await hub.async_stop()


async def test_watchdog_reconnects_a_device_that_came_back(hass, farm: Farm):
    device = farm.devices[0]
    hub = await make_hub(hass, farm)
    try:
        await hub.async_send(IR)
        device.faults.offline = True
        for _ in range(const.FAILURE_THRESHOLD):
            await hub.async_send(IR)
        assert hub.status == STATUS_RECONNECTING

        device.faults.offline = False
        await hub.async_ensure_connection()
        await asyncio.sleep(const.BACKOFF_SECONDS[0] + 0.5)

        assert hub.status == STATUS_ONLINE
        assert device.stats.auths == 2
    finally:
        await hub.async_stop()


async def test_rebooted_device_is_re_authenticated_and_the_frame_arrives_once(
    hass, farm: Farm
):
    """'Control key expired' is the one failure where re-sending is safe."""
    device = farm.devices[0]
    hub = await make_hub(hass, farm)
    try:
        await hub.async_send(IR)
        device.reset()  # counters now cover only what happens after the reboot
        device.reboot()

        assert await hub.async_send(IR) is True
        assert len(device.ir_log) == 1, "the frame must not be transmitted twice"
        assert device.stats.auths == 1, "the hub must handshake again, exactly once"
    finally:
        await hub.async_stop()


async def test_sensor_readings_are_published(hass, farm: Farm):
    device = farm.devices[0]
    device.temperature = 21.5
    device.humidity = 48.0
    hub = await make_hub(hass, farm)
    try:
        await hub.async_heartbeat()

        assert hub.has_sensor is True
        assert hub.sensors["temperature"] == pytest.approx(21.5)
        assert hub.sensors["humidity"] == pytest.approx(48.0)
    finally:
        await hub.async_stop()


async def test_a_device_without_a_sensor_is_still_watched(hass, farm: Farm):
    """RM4 mini has no sensor; liveness must not depend on one."""
    device = farm.devices[1]
    device.faults.zero_sensor = True
    hub = await make_hub(hass, farm, 1)
    try:
        await hub.async_heartbeat()
        assert hub.has_sensor is False
        assert hub.sensors == {}

        requests_before = device.stats.requests
        await hub.async_heartbeat()
        assert device.stats.requests > requests_before
        assert hub.status == STATUS_ONLINE
    finally:
        await hub.async_stop()


async def test_hub_identity_follows_the_hardware(hass, farm: Farm):
    hub = await make_hub(hass, farm)
    try:
        await hub.async_send(IR)
        assert hub.mac == farm.devices[0].mac
        assert hub.hub_id == farm.devices[0].mac.hex()
        assert hub.model == "RM4 pro"

        described = hub.describe()
        assert described["status"] == STATUS_ONLINE
        assert "key" not in str(described).lower()
    finally:
        await hub.async_stop()


def test_the_emitter_pause_stays_sane():
    """A future 'optimisation' must not delete the gap between transmissions."""
    assert const.IR_GAP_MS >= 100
    assert const.FAILURE_THRESHOLD >= 2
    assert const.BACKOFF_SECONDS[0] >= 1
