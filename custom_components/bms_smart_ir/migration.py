"""Upgrading an installation that is already in service.

Sites are running the previous release, so nothing here may change an entity
id, a unique id or a device the user can see. The only things that move are
duplicates the old model could not avoid.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import BACKEND_BROADLINK, CONF_BACKEND, CONF_PORT, DEFAULT_PORT
from .hub_device import is_hub_owner

_LOGGER = logging.getLogger(__package__)

DUPLICATE_SUFFIXES = ("_temperature", "_humidity")


async def async_migrate_v1_to_v2(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = {**entry.data}

    if data.get(CONF_BACKEND) == BACKEND_BROADLINK:
        # There was no way to set a port before; everything used the default.
        data.setdefault(CONF_PORT, DEFAULT_PORT)
        _async_drop_duplicate_readings(hass, entry)

    hass.config_entries.async_update_entry(entry, data=data, version=2)


def _async_drop_duplicate_readings(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the copies of one physical sensor that the old model created.

    Every appliance behind an emitter used to publish that emitter's
    temperature and humidity, so a room with three air conditioners had three
    identical thermometers. The owning entry keeps its pair — history and all;
    the rest are removed.
    """
    if is_hub_owner(hass, entry):
        return

    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.unique_id.endswith(DUPLICATE_SUFFIXES):
            _LOGGER.info(
                "Removing duplicate reading %s: its Broadlink already has one",
                entity.entity_id,
            )
            registry.async_remove(entity.entity_id)
