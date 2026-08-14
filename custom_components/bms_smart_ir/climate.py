"""Climate platform: a Broadlink air conditioner or a Tuya cloud one."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .bl_climate import async_build_climate
from .const import (
    BACKEND_BROADLINK,
    CONF_BACKEND,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_INFRARED_ID,
    CONF_NAME,
    DEVICE_TYPE_MEDIA_PLAYER,
    DOMAIN,
    KIND_CLIMATE,
)
from .tuya_climate import TuyaClimate

_LOGGER = logging.getLogger(__package__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create the climate entity matching the entry's backend."""
    if entry.data.get(CONF_BACKEND) == BACKEND_BROADLINK:
        if entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_MEDIA_PLAYER:
            return
        runtime = hass.data[DOMAIN]["entries"][entry.entry_id]
        entity = await async_build_climate(
            hass, entry, runtime["hub"], runtime["config"]
        )
        async_add_entities([entity])
        return

    data = entry.runtime_data
    if not isinstance(data, dict) or data.get("kind") != KIND_CLIMATE:
        return
    async_add_entities(
        [
            TuyaClimate(
                coordinator=data["coordinator"],
                cloud=data["cloud"],
                infrared_id=entry.data[CONF_INFRARED_ID],
                device_id=entry.data[CONF_DEVICE_ID],
                name=entry.data.get(CONF_NAME) or "IR Air Conditioner",
            )
        ]
    )
