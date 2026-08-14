"""BMS Smart IR — Broadlink and Tuya infrared control for Home Assistant.

One config entry is one appliance (an air conditioner, a television). Several
entries can sit behind the same Broadlink; they share a single connection to it
through `hub.BroadlinkHub`, which is the difference between an installation that
works with sixty emitters and one that does not.

The Tuya path is unchanged: it talks to the cloud, has no emitter of its own,
and is set up exactly as before.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .cloud import TuyaIRCloud
from .const import (
    BACKEND_BROADLINK,
    CONF_AREA,
    CONF_BACKEND,
    CONF_BMS_ENTRY_ID,
    CONF_CATEGORY_ID,
    CONF_CATEGORY_NAME,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_HOST,
    CONF_INFRARED_ID,
    CONF_KIND,
    CONF_PORT,
    CONF_TIMEOUT,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    DEVICE_TYPE_MEDIA_PLAYER,
    DOMAIN,
    KIND_CLIMATE,
)
from .coordinator import IRACoordinator
from .helpers import find_bms_creds
from .hub import async_get_hub, async_release_hub, async_stop_watchdog
from .hub_device import async_register_hub_device
from .panel import async_setup_panel
from .websocket import async_register_websocket_api

_LOGGER = logging.getLogger(__name__)

DATA_ENTRIES = "entries"


def _platforms_for(entry: ConfigEntry) -> list[Platform]:
    if entry.data.get(CONF_BACKEND) == BACKEND_BROADLINK:
        appliance = (
            Platform.MEDIA_PLAYER
            if entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_MEDIA_PLAYER
            else Platform.CLIMATE
        )
        # Sensors belong to the emitter; only its owning entry creates them,
        # but every entry forwards the platform so ownership can move.
        return [appliance, Platform.SENSOR, Platform.BINARY_SENSOR]
    if entry.data.get(CONF_KIND) == KIND_CLIMATE:
        return [Platform.CLIMATE]
    return [Platform.REMOTE, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data.setdefault(DATA_ENTRIES, {})
    await async_setup_panel(hass)
    async_register_websocket_api(hass)

    if entry.data.get(CONF_BACKEND) == BACKEND_BROADLINK:
        return await _setup_broadlink(hass, entry)
    return await _setup_tuya(hass, entry)


async def _setup_broadlink(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    config = {**entry.data, **entry.options}
    host = config.get(CONF_HOST)
    if not host:
        raise ConfigEntryNotReady("No Broadlink address configured")

    hub = await async_get_hub(
        hass,
        host,
        port=config.get(CONF_PORT, DEFAULT_PORT),
        timeout=config.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
    )
    hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id] = {"hub": hub, "config": config}

    # The device card exists before the first connection, so an emitter that is
    # offline right now is still visible in the panel and in Home Assistant.
    async_register_hub_device(hass, entry, hub)
    entry.async_on_unload(
        hub.async_add_listener(
            lambda: async_register_hub_device(hass, entry, hub)
        )
    )

    await hass.config_entries.async_forward_entry_setups(entry, _platforms_for(entry))
    _async_apply_area(hass, entry, config)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


@callback
def _async_apply_area(hass: HomeAssistant, entry: ConfigEntry, config: dict) -> None:
    area_id = config.get(CONF_AREA)
    if not area_id:
        return
    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    if device and device.area_id != area_id:
        registry.async_update_device(device.id, area_id=area_id)


async def _setup_tuya(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    creds = find_bms_creds(hass, entry.data.get(CONF_BMS_ENTRY_ID))
    if creds is None:
        raise ConfigEntryNotReady(
            "No Tuya cloud credentials found in the BMS Integration."
        )

    session = async_get_clientsession(hass)
    cloud = TuyaIRCloud(
        session, creds.region, creds.client_id, creds.secret, creds.user_id
    )
    infrared_id = entry.data[CONF_INFRARED_ID]
    device_id = entry.data[CONF_DEVICE_ID]

    if entry.data.get(CONF_KIND) == KIND_CLIMATE:
        coordinator = IRACoordinator(hass, entry, cloud, infrared_id, device_id)
        await coordinator.async_refresh()
        entry.runtime_data = {
            "kind": KIND_CLIMATE,
            "cloud": cloud,
            "coordinator": coordinator,
        }
    else:
        category_id, keys, message = await cloud.list_keys(infrared_id, device_id)
        if message != "ok":
            _LOGGER.warning(
                "Could not fetch keys for %s: %s (remote will have no buttons yet)",
                device_id,
                message,
            )
        entry.runtime_data = {
            "kind": "remote",
            "cloud": cloud,
            "category_id": category_id or entry.data.get(CONF_CATEGORY_ID),
            "category_name": entry.data.get(CONF_CATEGORY_NAME),
            "keys": keys,
        }

    await hass.config_entries.async_forward_entry_setups(entry, _platforms_for(entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, _platforms_for(entry)
    )
    if not unloaded:
        # Reporting success here would leave live entities behind a config
        # entry Home Assistant believes is gone, and the next load would build
        # a second set on top.
        return False

    if entry.data.get(CONF_BACKEND) == BACKEND_BROADLINK:
        runtime = hass.data[DOMAIN][DATA_ENTRIES].pop(entry.entry_id, None)
        if runtime is not None:
            hub = runtime["hub"]
            await async_release_hub(hass, hub.host, hub.port)
        if not hass.data[DOMAIN][DATA_ENTRIES]:
            await async_stop_watchdog(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bring an entry written by an older release up to date.

    Version 1 entries have no port (there was no way to set one) and, when
    several appliances share an emitter, a set of duplicate temperature and
    humidity entities — one per appliance for a single physical sensor.
    """
    if entry.version >= 2:
        return True

    from .migration import async_migrate_v1_to_v2

    await async_migrate_v1_to_v2(hass, entry)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
