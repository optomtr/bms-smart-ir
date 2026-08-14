"""Temperature and humidity reported by the Broadlink itself.

These belong to the emitter, not to the appliance in front of it: one RM4 pro
in a room has one thermometer, however many air conditioners and televisions it
drives. Only one config entry per address creates them, chosen deterministically
so a restart never hands the job to a different entry.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BACKEND_BROADLINK, CONF_BACKEND, DOMAIN
from .hub import BroadlinkHub
from .hub_device import hub_device_info, is_hub_owner

_LOGGER = logging.getLogger(__package__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_BACKEND) != BACKEND_BROADLINK:
        return
    if not is_hub_owner(hass, entry):
        return

    hub = hass.data[DOMAIN]["entries"][entry.entry_id]["hub"]
    added = False

    @callback
    def _add_when_known() -> None:
        """An RM4 mini has no thermometer; do not give it dead entities.

        Whether the accessory is there is only known after the first reading,
        which may be minutes away on a box that is offline right now — so the
        entities appear when the answer arrives, not before.
        """
        nonlocal added
        if added or not hub.has_sensor:
            return
        added = True
        async_add_entities(
            [
                BroadlinkReading(hub, entry, "temperature"),
                BroadlinkReading(hub, entry, "humidity"),
            ]
        )

    entry.async_on_unload(hub.async_add_listener(_add_when_known))
    _add_when_known()


class BroadlinkReading(SensorEntity):
    """One reading from the emitter's built-in sensor."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub: BroadlinkHub, entry: ConfigEntry, kind: str) -> None:
        self._hub = hub
        self._kind = kind
        # Unchanged from the previous release so the recorded history of an
        # installed site carries over.
        self._attr_unique_id = f"{entry.entry_id}_{kind}"
        if kind == "temperature":
            self._attr_name = "Температура"
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        else:
            self._attr_name = "Влажность"
            self._attr_device_class = SensorDeviceClass.HUMIDITY
            self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_device_info = hub_device_info(hub)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._hub.async_add_listener(self._async_updated))

    @callback
    def _async_updated(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """A model without the accessory has no reading to be unavailable about."""
        return self._hub.available and self._kind in self._hub.sensors

    @property
    def native_value(self) -> float | None:
        value = self._hub.sensors.get(self._kind)
        if value is None:
            return None
        return round(float(value), 1)
