"""An air conditioner in front of a Broadlink."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_OFF,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from .codes import async_load_code
from .const import (
    CONF_DEVICE_CODE,
    CONF_HUMIDITY_SENSOR,
    CONF_POWER_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    DEVICE_TYPE_CLIMATE,
)
from .entity import BmsIrEntity
from .hub import BroadlinkHub

_LOGGER = logging.getLogger(__package__)

HVAC_MODE_MAP = {
    "off": HVACMode.OFF,
    "heat": HVACMode.HEAT,
    "cool": HVACMode.COOL,
    "heat_cool": HVACMode.HEAT_COOL,
    "auto": HVACMode.AUTO,
    "dry": HVACMode.DRY,
    "fan_only": HVACMode.FAN_ONLY,
    "fan": HVACMode.FAN_ONLY,
}
HVAC_KEY_MAP = {
    HVACMode.HEAT: "heat",
    HVACMode.COOL: "cool",
    HVACMode.HEAT_COOL: "heat_cool",
    HVACMode.AUTO: "auto",
    HVACMode.DRY: "dry",
    HVACMode.FAN_ONLY: "fan_only",
}

OFF_STATES = (STATE_OFF, STATE_UNKNOWN, STATE_UNAVAILABLE, "off", None)


async def async_build_climate(
    hass: HomeAssistant, entry: ConfigEntry, hub: BroadlinkHub, config: dict[str, Any]
) -> BroadlinkClimate:
    """Build the entity, or make Home Assistant retry.

    A missing code file used to mean "no entity", which silently broke every
    automation pointing at it. Refusing to finish setup keeps the entity id
    registered and unavailable, and Home Assistant retries by itself.
    """
    code = config[CONF_DEVICE_CODE]
    device_data = await async_load_code(hass, DEVICE_TYPE_CLIMATE, code)
    if not device_data:
        raise ConfigEntryNotReady(
            f"IR code {code} is not available yet (no local copy and no download)"
        )
    return BroadlinkClimate(hub, entry, config, device_data)


class BroadlinkClimate(BmsIrEntity, ClimateEntity, RestoreEntity):
    """An IR-controlled air conditioner."""

    def __init__(self, hub, entry, config, device_data) -> None:
        super().__init__(hub, entry, config, device_data)

        self._commands: dict = device_data.get("commands", {})
        self._precision = float(device_data.get("precision", 1.0))
        self._attr_min_temp = float(device_data.get("minTemperature", 16))
        self._attr_max_temp = float(device_data.get("maxTemperature", 30))
        self._attr_target_temperature_step = self._precision
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS

        operation_modes = device_data.get("operationModes", [])
        self._attr_hvac_modes = [HVACMode.OFF] + [
            HVAC_MODE_MAP[mode] for mode in operation_modes if mode in HVAC_MODE_MAP
        ]
        self._attr_fan_modes = device_data.get("fanModes") or None
        self._attr_swing_modes = device_data.get("swingModes") or None

        features = ClimateEntityFeature.TARGET_TEMPERATURE
        features |= ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if self._attr_fan_modes:
            features |= ClimateEntityFeature.FAN_MODE
        if self._attr_swing_modes:
            features |= ClimateEntityFeature.SWING_MODE
        self._attr_supported_features = features

        self._attr_hvac_mode = HVACMode.OFF
        self._last_on_mode = next(
            (mode for mode in self._attr_hvac_modes if mode != HVACMode.OFF),
            HVACMode.COOL,
        )
        self._attr_target_temperature = (self._attr_min_temp + self._attr_max_temp) // 2
        self._attr_fan_mode = self._attr_fan_modes[0] if self._attr_fan_modes else None
        self._attr_swing_mode = (
            self._attr_swing_modes[0] if self._attr_swing_modes else None
        )

        self._temperature_sensor = config.get(CONF_TEMPERATURE_SENSOR)
        self._humidity_sensor = config.get(CONF_HUMIDITY_SENSOR)
        self._power_sensor = config.get(CONF_POWER_SENSOR)

    # ---- lifecycle -------------------------------------------------------
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        if (last := await self.async_get_last_state()) is not None:
            if last.state in [mode.value for mode in self._attr_hvac_modes]:
                self._attr_hvac_mode = HVACMode(last.state)
                if self._attr_hvac_mode != HVACMode.OFF:
                    self._last_on_mode = self._attr_hvac_mode
            if (temperature := last.attributes.get(ATTR_TEMPERATURE)) is not None:
                self._attr_target_temperature = float(temperature)
            if (fan := last.attributes.get("fan_mode")) in (self._attr_fan_modes or []):
                self._attr_fan_mode = fan
            if (swing := last.attributes.get("swing_mode")) in (
                self._attr_swing_modes or []
            ):
                self._attr_swing_mode = swing

        for entity_id, handler in (
            (self._temperature_sensor, self._async_temperature_changed),
            (self._humidity_sensor, self._async_humidity_changed),
            (self._power_sensor, self._async_power_changed),
        ):
            if entity_id:
                self.async_on_remove(
                    async_track_state_change_event(self.hass, entity_id, handler)
                )
        if self._temperature_sensor:
            self._read_temperature(self.hass.states.get(self._temperature_sensor))
        if self._humidity_sensor:
            self._read_humidity(self.hass.states.get(self._humidity_sensor))

    # ---- readings --------------------------------------------------------
    @property
    def current_temperature(self) -> float | None:
        """A linked sensor wins; otherwise the Broadlink's own sensor is used."""
        if self._temperature_sensor:
            return self._attr_current_temperature
        return self._hub.sensors.get("temperature")

    @property
    def current_humidity(self) -> float | None:
        if self._humidity_sensor:
            return self._attr_current_humidity
        humidity = self._hub.sensors.get("humidity")
        return None if humidity is None else int(humidity)

    @callback
    def _async_temperature_changed(self, event: Event) -> None:
        self._read_temperature(event.data.get("new_state"))
        self.async_write_ha_state()

    @callback
    def _async_humidity_changed(self, event: Event) -> None:
        self._read_humidity(event.data.get("new_state"))
        self.async_write_ha_state()

    @callback
    def _async_power_changed(self, event: Event) -> None:
        """Follow a power meter that proves the unit is really off."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        if new_state.state in OFF_STATES and self._attr_hvac_mode != HVACMode.OFF:
            self._attr_hvac_mode = HVACMode.OFF
            self.async_write_ha_state()

    def _read_temperature(self, state) -> None:
        if state and state.state not in OFF_STATES:
            try:
                self._attr_current_temperature = float(state.state)
            except ValueError:
                pass

    def _read_humidity(self, state) -> None:
        if state and state.state not in OFF_STATES:
            try:
                self._attr_current_humidity = float(state.state)
            except ValueError:
                pass

    # ---- commands --------------------------------------------------------
    def _snapshot(self) -> dict[str, Any]:
        return {
            "_attr_hvac_mode": self._attr_hvac_mode,
            "_attr_target_temperature": self._attr_target_temperature,
            "_attr_fan_mode": self._attr_fan_mode,
            "_attr_swing_mode": self._attr_swing_mode,
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        snapshot = self._snapshot()
        if (mode := kwargs.get("hvac_mode")) is not None:
            self._attr_hvac_mode = HVACMode(mode)
        self._attr_target_temperature = float(temperature)
        await self._async_apply(snapshot)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        snapshot = self._snapshot()
        self._attr_hvac_mode = hvac_mode
        if hvac_mode != HVACMode.OFF:
            self._last_on_mode = hvac_mode
        await self._async_apply(snapshot)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        snapshot = self._snapshot()
        self._attr_fan_mode = fan_mode
        await self._async_apply(snapshot)

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        snapshot = self._snapshot()
        self._attr_swing_mode = swing_mode
        await self._async_apply(snapshot)

    async def async_turn_on(self) -> None:
        snapshot = self._snapshot()
        self._attr_hvac_mode = self._last_on_mode
        await self._async_apply(snapshot)

    async def async_turn_off(self) -> None:
        snapshot = self._snapshot()
        self._attr_hvac_mode = HVACMode.OFF
        await self._async_apply(snapshot)

    async def _async_apply(self, snapshot: dict[str, Any]) -> None:
        """Send the whole state; repeats of it collapse into one transmission."""
        await self.async_send_with_rollback(
            self._resolve_command(),
            snapshot,
            coalesce_key=f"climate:{self._entry.entry_id}",
        )

    # ---- code lookup -----------------------------------------------------
    def _resolve_command(self) -> str | None:
        if self._attr_hvac_mode == HVACMode.OFF:
            return self._commands.get("off")

        node = self._commands.get(HVAC_KEY_MAP.get(self._attr_hvac_mode))
        path = [self._attr_fan_mode, self._attr_swing_mode, *self._temperature_keys()]
        return self._descend(node, [key for key in path if key is not None])

    def _descend(self, node: Any, keys: list[str]) -> str | None:
        """Walk a nested command dict, tolerating files that omit a level."""
        if isinstance(node, str):
            return node
        if not isinstance(node, dict):
            return None
        for key in keys:
            if key in node:
                result = self._descend(node[key], [k for k in keys if k != key])
                if result is not None:
                    return result
        if len(node) == 1:
            return self._descend(next(iter(node.values())), keys)
        return None

    def _temperature_keys(self) -> list[str]:
        temperature = self._attr_target_temperature
        keys = [f"{temperature:g}"]
        if float(temperature).is_integer():
            keys.append(str(int(temperature)))
        keys.append(str(temperature))
        seen: list[str] = []
        for key in keys:
            if key not in seen:
                seen.append(key)
        return seen
