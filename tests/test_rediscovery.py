"""Following a Broadlink across a DHCP lease change.

The premise the whole module rests on: an entity reads its emitter's address
off the live hub object (`entity.py`), never out of stored config — so moving
`hub.host`/`hub.port` in place is what actually restores control, and updating
config storage afterwards is only so a restart still finds the box. These
tests set up a real config entry and run the real setup path (`__init__.py`),
so the update-listener wiring that must skip a reload is exercised for real,
not assumed.

The one thing stubbed is the network broadcast itself
(`rediscovery.async_discover`): real hardware always answers on port 80, but
the bench's simulated devices share one loopback address on other ports (no
OS-level aliasing available here), so a real broadcast would find nothing.
Everything downstream of "who answered" — moving the hub, updating config,
reconnecting — runs against a real simulated device over a real socket.
"""

from __future__ import annotations

import asyncio

import pytest
from broadlink_sim import Farm

from custom_components.bms_smart_ir import const
from custom_components.bms_smart_ir.const import CONF_HOST, CONF_PORT
from custom_components.bms_smart_ir.hub import (
    STATUS_ONLINE,
    STATUS_RECONNECTING,
    BroadlinkHub,
    async_hubs,
    hub_key,
)
from custom_components.bms_smart_ir.rediscovery import async_follow_mac
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

IR = bytes.fromhex("260048004e1a1e1a1e341e1a1e000d05")


@pytest.fixture(autouse=True)
def _real_sockets(real_sockets):
    """These tests reconnect to a real simulated device at a new address."""


@pytest.fixture(autouse=True)
def _load_the_integration(enable_custom_integrations):
    """These tests run a real config entry through `async_setup_entry`, unlike
    the rest of the suite, which only builds `BroadlinkHub` objects directly —
    Home Assistant's component loader needs this fixture to find our package
    outside of a real Home Assistant install."""


@pytest.fixture
def farm():
    with Farm(2, base_port=24000, models=["rm4pro", "rm4pro"]) as running:
        yield running


async def setup_hub_entry(hass, host: str, port: int) -> MockConfigEntry:
    """A real, minimal Broadlink entry — a bare hub, no appliance."""
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        data={
            "backend": "broadlink",
            "controller": "Broadlink",
            "name": "Гостиная",
            CONF_HOST: host,
            CONF_PORT: port,
            "timeout": 1,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def stub_discovery_finds(monkeypatch, *, host: str, port: int, mac: str) -> None:
    """Make the broadcast scan answer as if only this one device were there.

    The fake keeps its own `port` kwarg (the real caller passes one) separate
    from the target port it returns — the two are unrelated, and a stub that
    conflated them would silently report whatever port it was scanned on.
    """
    target = {"host": host, "port": port, "mac": mac, "model": "RM4 pro", "name": "", "devtype": 0}

    async def _fake_discover(hass, *, timeout=4, port=const.DEFAULT_PORT):
        return [dict(target)]

    monkeypatch.setattr(
        "custom_components.bms_smart_ir.rediscovery.async_discover", _fake_discover
    )


def stub_discovery_finds_nothing(monkeypatch) -> None:
    async def _fake_discover(hass, *, timeout=4, port=const.DEFAULT_PORT):
        return []

    monkeypatch.setattr(
        "custom_components.bms_smart_ir.rediscovery.async_discover", _fake_discover
    )


async def test_wants_rediscovery_only_after_the_grace_window(hass, farm: Farm):
    host, port = farm.address(0)
    hub = BroadlinkHub(hass, host, port=port, timeout=1)
    hub.gap_ms = 20
    hub.mac_rediscover_after = 0.05
    hub.mac_rediscover_retry = 0.2
    await hub.async_start(with_heartbeat=False)
    assert await hub.async_send(IR) is True

    farm.devices[0].faults.offline = True
    for _ in range(const.FAILURE_THRESHOLD):
        await hub.async_send(IR)
    assert hub.status == STATUS_RECONNECTING

    assert hub.wants_mac_rediscovery() is False, "must wait out the grace window first"

    await asyncio.sleep(0.06)
    assert hub.wants_mac_rediscovery() is True

    hub.note_mac_rediscovery_attempt()
    assert hub.wants_mac_rediscovery() is False, "cooldown must apply right after a try"
    await hub.async_stop()


async def test_wants_rediscovery_requires_a_known_mac(hass, farm: Farm):
    host, port = farm.address(0)
    hub = BroadlinkHub(hass, host, port=port, timeout=1)
    hub.mac_rediscover_after = 0
    hub.gap_ms = 20
    await hub.async_start(with_heartbeat=False)
    farm.devices[0].faults.offline = True
    for _ in range(const.FAILURE_THRESHOLD):
        await hub.async_send(IR)

    assert hub.mac is None, "the box has never answered, so its MAC is unknown"
    assert hub.wants_mac_rediscovery() is False
    await hub.async_stop()


async def test_a_new_hub_can_be_seeded_with_a_known_mac(hass, farm: Farm):
    """After a restart, a hub built from stored config already knows its MAC —
    it does not have to connect once first to be eligible for rediscovery."""
    host, port = farm.address(0)
    hub = BroadlinkHub(hass, host, port=port, timeout=1, mac="AA:BB:CC:DD:EE:FF")

    assert hub.mac == bytes.fromhex("AABBCCDDEEFF")
    assert hub.mac_text == "AA:BB:CC:DD:EE:FF"


async def test_follow_mac_relocates_a_hub_that_moved(hass, farm: Farm, monkeypatch):
    """The whole point: control must work again, with no entity ever reloaded."""
    old_host, old_port = farm.address(0)
    new_host, new_port = farm.address(1)

    entry = await setup_hub_entry(hass, old_host, old_port)
    hub = async_hubs(hass)[hub_key(old_host, old_port)]
    hub.gap_ms = 20
    assert await hub.async_send(IR) is True  # a real handshake, a real MAC
    mac_text = hub.mac_text
    assert mac_text is not None

    reloaded: list[str] = []

    async def _fake_reload(entry_id):
        reloaded.append(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_reload", _fake_reload)
    stub_discovery_finds(monkeypatch, host=new_host, port=new_port, mac=mac_text)

    # Simulate the SAME physical box now answering at the second address: the
    # bench models two devices as two ports, each with its own MAC, so the
    # "same box, new address" premise needs the new port to answer with the
    # old device's MAC, exactly as real hardware would.
    farm.devices[1].mac = farm.devices[0].mac
    farm.devices[0].faults.offline = True  # the box "moved" off the old address

    moved = await async_follow_mac(hass, hub)

    assert moved is True
    assert (hub.host, hub.port) == (new_host, new_port)
    assert async_hubs(hass).get(hub_key(old_host, old_port)) is None
    assert async_hubs(hass)[hub_key(new_host, new_port)] is hub
    assert entry.data[CONF_HOST] == new_host
    assert entry.data[CONF_PORT] == new_port
    assert entry.state is ConfigEntryState.LOADED
    assert reloaded == [], "a background move must not reload the entry"

    # Control must already work at the new address, with no extra setup.
    assert await hub.async_send(IR) is True
    assert farm.devices[1].ir_log, "the command must have reached the new box"
    assert hub.status == STATUS_ONLINE
    assert hub.mac_text == mac_text, "identity must not change, only the address"

    await hass.config_entries.async_unload(entry.entry_id)


async def test_follow_mac_does_nothing_when_no_match_is_found(hass, farm: Farm, monkeypatch):
    host, port = farm.address(0)
    entry = await setup_hub_entry(hass, host, port)
    hub = async_hubs(hass)[hub_key(host, port)]
    hub.gap_ms = 20
    assert await hub.async_send(IR) is True

    stub_discovery_finds_nothing(monkeypatch)
    farm.devices[0].faults.offline = True

    moved = await async_follow_mac(hass, hub)

    assert moved is False
    assert (hub.host, hub.port) == (host, port)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_follow_mac_ignores_an_answer_at_the_same_address(
    hass, farm: Farm, monkeypatch
):
    """A device answering again at its own address is a reconnect, not a move."""
    host, port = farm.address(0)
    entry = await setup_hub_entry(hass, host, port)
    hub = async_hubs(hass)[hub_key(host, port)]
    hub.gap_ms = 20
    assert await hub.async_send(IR) is True

    stub_discovery_finds(monkeypatch, host=host, port=port, mac=hub.mac_text)

    moved = await async_follow_mac(hass, hub)

    assert moved is False
    await hass.config_entries.async_unload(entry.entry_id)


async def test_mac_is_persisted_silently_after_the_first_connect(hass, farm: Farm, monkeypatch):
    """Learning a MAC must reach config storage without reloading the entry —
    the whole point is that a restart can use it later, not that anything
    about the running installation changes right now."""
    host, port = farm.address(0)
    entry = await setup_hub_entry(hass, host, port)

    reloaded: list[str] = []

    async def _fake_reload(entry_id):
        reloaded.append(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_reload", _fake_reload)

    hub = async_hubs(hass)[hub_key(host, port)]
    hub.gap_ms = 20
    assert await hub.async_send(IR) is True
    await hass.async_block_till_done()

    assert entry.data[const.CONF_MAC] == hub.mac_text
    assert reloaded == []
    await hass.config_entries.async_unload(entry.entry_id)
