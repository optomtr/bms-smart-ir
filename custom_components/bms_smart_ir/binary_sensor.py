"""Is the emitter reachable — recorded, so the panel can draw uptime.

This entity is deliberately never "unavailable": it is the thing that reports
availability, so a gap in its history would mean the recorder lost the very
minutes worth looking at.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BACKEND_BROADLINK, CONF_BACKEND, DOMAIN
from .hub import STATUS_ONLINE, BroadlinkHub
from .hub_device import hub_device_info, is_hub_owner


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_BACKEND) != BACKEND_BROADLINK:
        return
    if not is_hub_owner(hass, entry):
        return

    hub = hass.data[DOMAIN]["entries"][entry.entry_id]["hub"]
    async_add_entities([BroadlinkOnline(hub, entry)])


class BroadlinkOnline(BinarySensorEntity):
    """Connectivity of one Broadlink."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "Связь"

    def __init__(self, hub: BroadlinkHub, entry: ConfigEntry) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry.entry_id}_online"
        self._attr_device_info = hub_device_info(hub)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._hub.async_add_listener(self._async_updated))

    @callback
    def _async_updated(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._hub.status == STATUS_ONLINE

    @property
    def extra_state_attributes(self) -> dict:
        stats = self._hub.stats
        return {
            "статус": self._hub.status,
            "адрес": self._hub.host,
            "модель": self._hub.model,
            "mac": self._hub.mac_text,
            "команд отправлено": stats.sent,
            "команд потеряно": stats.failed,
            "переподключений": stats.reconnects,
        }
