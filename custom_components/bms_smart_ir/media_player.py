"""A television in front of a Broadlink."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .codes import async_load_code
from .const import (
    BACKEND_BROADLINK,
    CONF_BACKEND,
    CONF_DEVICE_CODE,
    CONF_DEVICE_TYPE,
    DEVICE_TYPE_MEDIA_PLAYER,
    DOMAIN,
)
from .entity import BmsIrEntity

_LOGGER = logging.getLogger(__package__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_BACKEND) != BACKEND_BROADLINK:
        return
    if entry.data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_MEDIA_PLAYER:
        return

    runtime = hass.data[DOMAIN]["entries"][entry.entry_id]
    config = runtime["config"]
    device_data = await async_load_code(
        hass, DEVICE_TYPE_MEDIA_PLAYER, config[CONF_DEVICE_CODE]
    )
    if not device_data:
        raise ConfigEntryNotReady(
            f"IR code {config[CONF_DEVICE_CODE]} is not available yet"
        )

    async_add_entities([BroadlinkTV(runtime["hub"], entry, config, device_data)])


class BroadlinkTV(BmsIrEntity, MediaPlayerEntity, RestoreEntity):
    """An IR-controlled television."""

    _attr_device_class = MediaPlayerDeviceClass.TV

    def __init__(self, hub, entry, config, device_data) -> None:
        super().__init__(hub, entry, config, device_data)

        self._commands: dict = device_data.get("commands", {})
        sources = self._commands.get("sources")
        self._sources: dict = sources if isinstance(sources, dict) else {}
        self._attr_source_list = list(self._sources) or None
        self._attr_source = None
        self._attr_state = MediaPlayerState.OFF

        features = (
            MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.TURN_OFF
            | MediaPlayerEntityFeature.VOLUME_STEP
            | MediaPlayerEntityFeature.VOLUME_MUTE
            | MediaPlayerEntityFeature.PREVIOUS_TRACK
            | MediaPlayerEntityFeature.NEXT_TRACK
        )
        if self._sources:
            features |= MediaPlayerEntityFeature.SELECT_SOURCE
        self._attr_supported_features = features

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if last.state in (MediaPlayerState.ON, MediaPlayerState.OFF):
                self._attr_state = MediaPlayerState(last.state)
            if (source := last.attributes.get("source")) in (
                self._attr_source_list or []
            ):
                self._attr_source = source

    # ---- commands --------------------------------------------------------
    def _snapshot(self) -> dict[str, Any]:
        return {"_attr_state": self._attr_state, "_attr_source": self._attr_source}

    async def _async_press(self, key: str) -> bool:
        """A button press. Never collapsed: two presses mean two presses."""
        command = self._commands.get(key)
        if not command:
            _LOGGER.warning("%s: no IR command for '%s'", self._attr_name, key)
            return False
        return await self.async_send_with_rollback(command, self._snapshot())

    async def async_turn_on(self) -> None:
        snapshot = self._snapshot()
        self._attr_state = MediaPlayerState.ON
        await self.async_send_with_rollback(self._commands.get("on"), snapshot)

    async def async_turn_off(self) -> None:
        snapshot = self._snapshot()
        self._attr_state = MediaPlayerState.OFF
        await self.async_send_with_rollback(self._commands.get("off"), snapshot)

    async def async_volume_up(self) -> None:
        await self._async_press("volumeUp")

    async def async_volume_down(self) -> None:
        await self._async_press("volumeDown")

    async def async_mute_volume(self, mute: bool) -> None:
        await self._async_press("mute")

    async def async_media_previous_track(self) -> None:
        await self._async_press("previousChannel")

    async def async_media_next_track(self) -> None:
        await self._async_press("nextChannel")

    async def async_select_source(self, source: str) -> None:
        command = self._sources.get(source)
        if not command:
            _LOGGER.warning("%s: unknown source '%s'", self._attr_name, source)
            return
        snapshot = self._snapshot()
        self._attr_source = source
        if self._attr_state == MediaPlayerState.OFF:
            self._attr_state = MediaPlayerState.ON
        await self.async_send_with_rollback(command, snapshot)
