"""The integration end to end: config entry in, IR frames out.

Everything runs against the simulator over a real socket, so these tests cover
the seams — config entry, hub, entity, device registry — not just the pieces.
"""

from __future__ import annotations

import json
import os

import pytest
from broadlink_sim import Farm
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.bms_smart_ir.const import DOMAIN

# A minimal but real SmartIR-shaped code file: cool mode, two fan speeds,
# three temperatures, plus an off command.
def _commands() -> dict:
    """A SmartIR-shaped command tree covering every temperature, as real
    files do — a file with gaps would make the entity roll back correctly and
    the test fail for the wrong reason."""
    tree = {"off": "JgBIAE4aHhoeNB4aHgANBQ=="}
    for mode_index, mode in enumerate(("cool", "heat")):
        tree[mode] = {}
        for fan_index, fan in enumerate(("auto", "high")):
            tree[mode][fan] = {
                str(temperature): "JgBIAE4aHhoeNB4aHg%02dNBQ==" % (
                    mode_index * 20 + fan_index * 10 + temperature - 18
                )
                for temperature in range(18, 25)
            }
    return tree


CLIMATE_CODE = {
    "manufacturer": "BMS",
    "supportedModels": ["Стенд"],
    "commandsEncoding": "Base64",
    "minTemperature": 18,
    "maxTemperature": 24,
    "precision": 1,
    "operationModes": ["cool", "heat"],
    "fanModes": ["auto", "high"],
    "commands": _commands(),
}


@pytest.fixture(autouse=True)
def _real_sockets(real_sockets):
    """The whole point is to reach the simulator over the wire."""


@pytest.fixture(autouse=True)
def _custom_integration(enable_custom_integrations):
    """Let Home Assistant load custom_components/bms_smart_ir."""


@pytest.fixture(autouse=True)
def expected_lingering_threads() -> bool:
    """Home Assistant's own shutdown watcher outlives the test.

    `_run_safe_shutdown_loop` is started by Home Assistant when it stops with
    executor work in flight — our blocking library calls. It is a daemon thread
    belonging to the harness, not a leak in this integration, and the harness
    provides this fixture to say so.
    """
    return True


@pytest.fixture(autouse=True)
async def _unload_after_test(hass):
    """Stop the hubs before the harness checks for stray tasks and threads."""
    yield
    for entry in list(hass.config_entries.async_entries(DOMAIN)):
        await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def farm():
    with Farm(3, base_port=24000, models=["rm4pro", "rm4mini", "rm4c"]) as running:
        yield running


@pytest.fixture
def code_file(hass):
    """Put the test code where the integration keeps user code files."""
    path = hass.config.path("bms_smart_ir_codes", "climate")
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "9001.json"), "w", encoding="utf-8") as handle:
        json.dump(CLIMATE_CODE, handle)
    return "9001"


def make_entry(farm: Farm, index: int, *, name: str, code: str = "9001") -> MockConfigEntry:
    host, port = farm.address(index)
    return MockConfigEntry(
        domain=DOMAIN,
        title=name,
        version=2,
        unique_id=f"climate_{host}_{port}_{name}",
        data={
            "backend": "broadlink",
            "controller": "Broadlink",
            "name": name,
            "host": host,
            "port": port,
            "timeout": 1,
            "device_type": "climate",
            "device_code": code,
            "manufacturer": "BMS",
            "model": "Стенд",
        },
    )


async def setup_entry(hass, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_air_conditioner_appears_and_transmits(hass, farm, code_file):
    entry = make_entry(farm, 0, name="Кондиционер стенд")
    await setup_entry(hass, entry)

    state = hass.states.get("climate.konditsioner_stend")
    assert state is not None, "the entity has to exist even before the first command"
    assert state.state == STATE_OFF

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {ATTR_ENTITY_ID: "climate.konditsioner_stend", "temperature": 22, "hvac_mode": "cool"},
        blocking=True,
    )

    log = farm.devices[0].ir_log
    assert len(log) == 1, "one state change is one transmission"
    state = hass.states.get("climate.konditsioner_stend")
    assert state.state == "cool"
    assert state.attributes["temperature"] == 22


async def test_a_lost_command_does_not_leave_a_false_state(hass, farm, code_file):
    """The defect that made the interface lie: this is the regression test."""
    entry = make_entry(farm, 0, name="Кондиционер стенд")
    await setup_entry(hass, entry)
    farm.devices[0].faults.offline = True

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {ATTR_ENTITY_ID: "climate.konditsioner_stend", "temperature": 24, "hvac_mode": "heat"},
        blocking=True,
    )

    state = hass.states.get("climate.konditsioner_stend")
    assert state.state == STATE_OFF, "a command that never arrived must roll back"
    assert not farm.devices[0].ir_log


async def test_two_appliances_on_one_emitter_share_the_session(hass, farm, code_file):
    first = make_entry(farm, 0, name="Кондиционер один")
    second = make_entry(farm, 0, name="Кондиционер два")
    await setup_entry(hass, first)
    await setup_entry(hass, second)

    for entity_id in ("climate.konditsioner_odin", "climate.konditsioner_dva"):
        await hass.services.async_call(
            "climate", "set_hvac_mode", {ATTR_ENTITY_ID: entity_id, "hvac_mode": "cool"}, blocking=True
        )

    assert len(farm.devices[0].ir_log) == 2
    assert farm.devices[0].stats.auths == 1, "one emitter, one handshake"


async def test_the_emitter_becomes_a_device_with_its_appliances_behind_it(
    hass, farm, code_file
):
    entry = make_entry(farm, 0, name="Кондиционер стенд")
    await setup_entry(hass, entry)
    # A transmission forces the connection, which is when the model is learnt.
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {ATTR_ENTITY_ID: "climate.konditsioner_stend", "hvac_mode": "cool"},
        blocking=True,
    )
    await hass.async_block_till_done()

    registry = dr.async_get(hass)
    host, port = farm.address(0)
    hub_device = registry.async_get_device(identifiers={(DOMAIN, f"hub:{host}:{port}")})
    assert hub_device is not None
    assert hub_device.model == "RM4 pro"

    appliance = registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert appliance.via_device_id == hub_device.id


async def test_climate_sensors_appear_only_where_the_hardware_has_them(
    hass, farm, code_file
):
    with_sensor = make_entry(farm, 0, name="Кондиционер с датчиком")   # RM4 pro
    without = make_entry(farm, 1, name="Кондиционер без датчика")      # RM4 mini
    farm.devices[1].faults.zero_sensor = True
    await setup_entry(hass, with_sensor)
    await setup_entry(hass, without)

    for entry in (with_sensor, without):
        hub = hass.data[DOMAIN]["entries"][entry.entry_id]["hub"]
        await hub.async_heartbeat()
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    on_pro = [
        item.unique_id
        for item in er.async_entries_for_config_entry(registry, with_sensor.entry_id)
    ]
    on_mini = [
        item.unique_id
        for item in er.async_entries_for_config_entry(registry, without.entry_id)
    ]
    assert f"{with_sensor.entry_id}_temperature" in on_pro
    assert f"{without.entry_id}_temperature" not in on_mini
    assert f"{without.entry_id}_online" in on_mini, "liveness is tracked either way"


async def test_migration_removes_the_duplicate_thermometers(hass, farm, code_file):
    """A v1 site with three appliances on one emitter had three thermometers."""
    host, port = farm.address(0)
    entries = []
    for index in range(3):
        entry = MockConfigEntry(
            domain=DOMAIN,
            title=f"Старый {index}",
            version=1,
            unique_id=f"climate_{host}_9001_{index}",
            data={
                "backend": "broadlink",
                "controller": "Broadlink",
                "name": f"Старый {index}",
                "host": host,
                "device_type": "climate",
                "device_code": "9001",
            },
        )
        entry.add_to_hass(hass)
        entries.append(entry)

    registry = er.async_get(hass)
    for entry in entries:
        for kind in ("temperature", "humidity"):
            registry.async_get_or_create(
                "sensor",
                DOMAIN,
                f"{entry.entry_id}_{kind}",
                config_entry=entry,
                suggested_object_id=f"staryi_{entry.entry_id[:4]}_{kind}",
            )

    from custom_components.bms_smart_ir import async_migrate_entry

    for entry in entries:
        assert await async_migrate_entry(hass, entry)
    await hass.async_block_till_done()

    survivors = [
        item
        for item in registry.entities.values()
        if item.unique_id.endswith(("_temperature", "_humidity"))
    ]
    assert len(survivors) == 2, "one physical sensor, one pair of entities"
    owner = min(entry.entry_id for entry in entries)
    assert all(item.config_entry_id == owner for item in survivors)
    assert all(entry.version == 2 for entry in entries)
    assert all(entry.data["port"] == 80 for entry in entries)
