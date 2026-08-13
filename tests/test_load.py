"""Load scenarios that stay in the suite for good.

An installation with sixty emitters is the case that breaks integrations, so it
is checked on every run rather than remembered after a complaint from a site.
The invariant under test is isolation: one device in trouble must never cost
the others their commands.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from broadlink_sim import Farm

from custom_components.bms_smart_ir.hub import STATUS_ONLINE, BroadlinkHub

DEVICES = 60
IR = bytes.fromhex("260048004e1a1e1a1e341e1a1e000d05")


@pytest.fixture(autouse=True)
def _real_sockets(real_sockets):
    """Every test in this file drives the simulator through a real socket."""


@pytest.fixture
def big_farm():
    with Farm(DEVICES, base_port=23000) as running:
        yield running


async def build_hubs(hass, farm: Farm) -> list[BroadlinkHub]:
    hubs = []
    for index in range(len(farm.devices)):
        host, port = farm.address(index)
        hub = BroadlinkHub(hass, host, port=port, timeout=1)
        hub.gap_ms = 20
        hub.sensor_interval = 3600
        await hub.async_start(with_heartbeat=False)
        hubs.append(hub)
    return hubs


async def stop_all(hubs: list[BroadlinkHub]) -> None:
    for hub in hubs:
        await hub.async_stop()


async def test_sixty_devices_all_answer(hass, big_farm: Farm):
    hubs = await build_hubs(hass, big_farm)
    try:
        started = time.monotonic()
        results = await asyncio.gather(*(hub.async_send(IR) for hub in hubs))
        elapsed = time.monotonic() - started

        assert all(results), f"{results.count(False)} of {DEVICES} commands lost"
        assert all(device.stats.auths == 1 for device in big_farm.devices)
        assert elapsed < 20, f"sixty devices took {elapsed:.1f}s to answer once"
    finally:
        await stop_all(hubs)


async def test_five_commands_to_every_device_arrive_complete(hass, big_farm: Farm):
    """300 transmissions at once — the 'turn everything off' scene."""
    hubs = await build_hubs(hass, big_farm)
    try:
        results = await asyncio.gather(
            *(
                hub.async_send(IR + bytes([index]))
                for hub in hubs
                for index in range(5)
            )
        )
        assert all(results)
        for device in big_farm.devices:
            assert len(device.ir_log) == 5
            assert min(device.ir_gaps_ms()) >= 16
    finally:
        await stop_all(hubs)


async def test_one_dead_device_does_not_cost_the_others_anything(
    hass, big_farm: Farm
):
    """The failure class the whole rework exists to prevent."""
    hubs = await build_hubs(hass, big_farm)
    big_farm.devices[0].faults.offline = True
    try:
        results = await asyncio.gather(*(hub.async_send(IR) for hub in hubs))

        assert results[0] is False
        assert all(results[1:]), "a dead device took working ones down with it"
        assert all(hub.status == STATUS_ONLINE for hub in hubs[1:])
    finally:
        await stop_all(hubs)


async def test_a_slow_device_does_not_hold_up_the_queue_of_another(
    hass, big_farm: Farm
):
    hubs = await build_hubs(hass, big_farm)
    big_farm.devices[0].faults.latency_ms = 400
    try:
        started = time.monotonic()
        fast = await hubs[1].async_send(IR)
        elapsed = time.monotonic() - started

        assert fast is True
        assert elapsed < 0.35, f"a slow neighbour delayed this device by {elapsed:.2f}s"
    finally:
        await stop_all(hubs)
