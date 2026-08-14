"""The commands the panel is built on.

The panel itself is thin: if these behave, the interface behaves. Each test
drives the same WebSocket API a browser would.
"""

from __future__ import annotations

import pytest
from broadlink_sim import Farm
from test_integration import CLIMATE_CODE, make_entry, setup_entry  # noqa: F401

import json
import os

from custom_components.bms_smart_ir.const import DOMAIN


@pytest.fixture(autouse=True)
def _real_sockets(real_sockets):
    """Discovery and test signals go over the wire."""


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
    with Farm(3, base_port=25000, models=["rm4pro", "rm4c", "rm4mini"]) as running:
        yield running


@pytest.fixture
def code_file(hass):
    path = hass.config.path("bms_smart_ir_codes", "climate")
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "9001.json"), "w", encoding="utf-8") as handle:
        json.dump(CLIMATE_CODE, handle)
    return "9001"


async def call(client, message_id: int, payload: dict) -> dict:
    await client.send_json({"id": message_id, **payload})
    return await client.receive_json()


async def test_overview_lists_emitters_with_their_appliances(
    hass, hass_ws_client, farm, code_file
):
    entry = make_entry(farm, 0, name="Кондиционер зал")
    await setup_entry(hass, entry)
    client = await hass_ws_client(hass)

    result = await call(client, 1, {"type": f"{DOMAIN}/overview"})

    assert result["success"]
    hubs = result["result"]["hubs"]
    assert len(hubs) == 1
    assert hubs[0]["appliances"][0]["name"] == "Кондиционер зал"
    assert result["result"]["totals"]["appliances"] == 1


async def test_probe_finds_an_emitter_by_address(hass, hass_ws_client, farm, code_file):
    entry = make_entry(farm, 0, name="Кондиционер зал")
    await setup_entry(hass, entry)
    client = await hass_ws_client(hass)
    host, port = farm.address(1)

    result = await call(
        client, 1, {"type": f"{DOMAIN}/probe", "host": f"{host}:{port}", "timeout": 2}
    )

    assert result["success"]
    device = result["result"]["devices"][0]
    assert device["model"] == "RM4C mini"
    assert device["known"] is False, "this one is not configured yet"


async def test_adding_an_appliance_from_the_panel_creates_it(
    hass, hass_ws_client, farm, code_file
):
    seed = make_entry(farm, 0, name="Кондиционер зал")
    await setup_entry(hass, seed)
    client = await hass_ws_client(hass)
    host, port = farm.address(1)

    result = await call(
        client,
        1,
        {
            "type": f"{DOMAIN}/add_appliance",
            "host": f"{host}:{port}",
            "device_type": "climate",
            "code": "9001",
            "name": "Кондиционер спальня",
            "manufacturer": "BMS",
            "model": "Стенд",
        },
    )
    await hass.async_block_till_done()

    assert result["success"], result
    entry = hass.config_entries.async_get_entry(result["result"]["entry_id"])
    assert entry.data["host"] == host
    assert entry.data["port"] == port
    assert hass.states.get("climate.konditsioner_spalnia") is not None


async def test_renaming_an_appliance_keeps_its_entity(
    hass, hass_ws_client, farm, code_file
):
    entry = make_entry(farm, 0, name="Кондиционер зал")
    await setup_entry(hass, entry)
    client = await hass_ws_client(hass)

    result = await call(
        client,
        1,
        {
            "type": f"{DOMAIN}/update_appliance",
            "entry_id": entry.entry_id,
            "name": "Кондиционер большой зал",
        },
    )
    await hass.async_block_till_done()

    assert result["success"]
    assert entry.data["name"] == "Кондиционер большой зал"
    state = hass.states.get("climate.konditsioner_zal")
    assert state is not None, "the entity id must not follow the label"
    assert state.attributes["friendly_name"] == "Кондиционер большой зал"


async def test_replacing_a_dead_emitter_moves_everything_to_the_new_one(
    hass, hass_ws_client, farm, code_file
):
    """The field repair: swap the box, keep the appliances."""
    first = make_entry(farm, 0, name="Кондиционер зал")
    second = make_entry(farm, 0, name="Кондиционер кухня")
    await setup_entry(hass, first)
    await setup_entry(hass, second)
    client = await hass_ws_client(hass)

    old_host, old_port = farm.address(0)
    new_host, new_port = farm.address(2)
    result = await call(
        client,
        1,
        {
            "type": f"{DOMAIN}/replace_hub",
            "host": f"{old_host}:{old_port}",
            "new_host": f"{new_host}:{new_port}",
        },
    )
    await hass.async_block_till_done()

    assert result["success"], result
    assert result["result"]["moved"] == 2
    assert first.data["port"] == new_port
    assert second.data["port"] == new_port
    assert hass.states.get("climate.konditsioner_zal") is not None
    assert hass.states.get("climate.konditsioner_kukhnia") is not None


async def test_a_test_signal_reaches_the_emitter(hass, hass_ws_client, farm, code_file):
    entry = make_entry(farm, 0, name="Кондиционер зал")
    await setup_entry(hass, entry)
    client = await hass_ws_client(hass)
    host, port = farm.address(0)

    result = await call(
        client,
        1,
        {
            "type": f"{DOMAIN}/test_code",
            "host": f"{host}:{port}",
            "device_type": "climate",
            "code": "9001",
        },
    )

    assert result["success"]
    assert result["result"]["delivered"] is True
    assert len(farm.devices[0].ir_log) == 1


async def test_the_catalogue_puts_common_brands_first(
    hass, hass_ws_client, farm, code_file
):
    entry = make_entry(farm, 0, name="Кондиционер зал")
    await setup_entry(hass, entry)
    client = await hass_ws_client(hass)

    result = await call(
        client, 1, {"type": f"{DOMAIN}/catalog", "device_type": "climate"}
    )

    assert result["success"]
    catalog = result["result"]["catalog"]
    assert catalog[0]["popular"] is True
    assert any(item["manufacturer"] == "Gree" for item in catalog)
    assert catalog[0]["models"], "a brand with no models would be a dead end"


async def test_changing_the_installation_needs_an_administrator(
    hass, hass_ws_client, hass_read_only_access_token, farm, code_file
):
    """Guarding this in the interface only would leave the command open."""
    entry = make_entry(farm, 0, name="Кондиционер зал")
    await setup_entry(hass, entry)
    client = await hass_ws_client(hass, hass_read_only_access_token)

    result = await call(
        client,
        1,
        {"type": f"{DOMAIN}/remove_appliance", "entry_id": entry.entry_id},
    )

    assert not result["success"]
    assert result["error"]["code"] == "unauthorized"
    assert hass.config_entries.async_get_entry(entry.entry_id) is not None
