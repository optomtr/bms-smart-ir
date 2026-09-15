"""The panel must exist after registering just a Broadlink, no appliance.

Home Assistant only calls `async_setup_entry` — which is what registers the
sidebar panel — once at least one config entry exists. Before this, the
config flow's only way to finish was the full manufacturer/model/test wizard,
so an installer who did not complete an appliance never got a config entry at
all, and the panel (which is where discovery and every later appliance are
meant to be added) never appeared. These tests drive the real config flow and
the real entry setup, the same way Home Assistant does.
"""

from __future__ import annotations

import pytest
from broadlink_sim import Farm

from custom_components.bms_smart_ir import const
from custom_components.bms_smart_ir.__init__ import _platforms_for
from custom_components.bms_smart_ir.const import (
    CONF_BACKEND,
    CONF_DEVICE_TYPE,
    CONF_HOST,
    CONF_PORT,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.data_entry_flow import FlowResultType

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def _real_sockets(real_sockets):
    """These tests drive the simulator through a real socket."""


@pytest.fixture
def farm():
    with Farm(1, base_port=25000, models=["rm4pro"]) as running:
        yield running


async def test_registering_a_bare_hub_creates_the_panel(hass, farm: Farm):
    """The exact bug report: add a Broadlink, add nothing behind it, and the
    sidebar must still appear."""
    host, port = farm.address(0)

    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.MENU

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "broadlink"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "Гостиная", "host": f"{host}:{port}", "timeout": 1},
    )
    assert result["step_id"] == "bl_type"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device_type": "hub_only"}
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.data[CONF_BACKEND] == "broadlink"
    assert CONF_DEVICE_TYPE not in entry.data
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.data[const.DOMAIN].get("panel_registered") is True

    # No appliance was ever added — only the emitter's own sensors may exist.
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert all(entity.domain in ("sensor", "binary_sensor") for entity in entities)


async def test_hub_only_platforms_have_nothing_to_control(hass):
    """A hub-only entry (no CONF_DEVICE_TYPE) must never forward CLIMATE or
    MEDIA_PLAYER — `bl_climate.py` would crash on the missing device code."""

    class _FakeEntry:
        data = {CONF_BACKEND: "broadlink", CONF_HOST: "127.0.0.1", CONF_PORT: 80}

    platforms = _platforms_for(_FakeEntry())
    assert Platform.CLIMATE not in platforms
    assert Platform.MEDIA_PLAYER not in platforms
    assert Platform.SENSOR in platforms
    assert Platform.BINARY_SENSOR in platforms


async def test_hub_only_entry_is_not_listed_as_an_appliance(hass, farm: Farm):
    """The panel's per-hub appliance list must not show a phantom card for
    the hub-only entry itself."""
    host, port = farm.address(0)
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN,
        context={"source": "import_hub"},
        data={CONF_HOST: host, CONF_PORT: port, "name": "Гостиная"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    from custom_components.bms_smart_ir.websocket import _appliances

    assert _appliances(hass, host, port) == []


async def test_registering_the_same_hub_twice_is_refused(hass, farm: Farm):
    host, port = farm.address(0)
    first = await hass.config_entries.flow.async_init(
        const.DOMAIN,
        context={"source": "import_hub"},
        data={CONF_HOST: host, CONF_PORT: port, "name": "Гостиная"},
    )
    assert first["type"] == FlowResultType.CREATE_ENTRY

    second = await hass.config_entries.flow.async_init(
        const.DOMAIN,
        context={"source": "import_hub"},
        data={CONF_HOST: host, CONF_PORT: port, "name": "Гостиная (дубль)"},
    )
    assert second["type"] == FlowResultType.ABORT
    assert second["reason"] == "already_configured"
