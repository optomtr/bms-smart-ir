"""Shared behaviour for every appliance driven through a hub.

Two things live here because getting them wrong is invisible until an
installation is a year old:

* availability follows the hub, so a Broadlink that dropped off Wi-Fi greys out
  its appliances instead of pretending to control them;
* optimistic state is applied with a snapshot, so a lost command can be rolled
  back honestly instead of leaving the interface showing something that never
  happened.
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import CONF_MANUFACTURER, CONF_MODEL, CONF_NAME, DOMAIN
from .hub import BroadlinkHub

_LOGGER = logging.getLogger(__package__)


class BmsIrEntity(Entity):
    """An appliance in front of one Broadlink."""

    # The previous release named entities this way. Changing it would rename
    # every entity on every installed site, so it stays.
    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(
        self,
        hub: BroadlinkHub,
        entry: ConfigEntry,
        config: dict[str, Any],
        device_data: dict[str, Any],
    ) -> None:
        self._hub = hub
        self._entry = entry
        self._config = config
        self._data = device_data

        self._attr_name = config[CONF_NAME]
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=config[CONF_NAME],
            manufacturer=device_data.get("manufacturer")
            or config.get(CONF_MANUFACTURER)
            or "BMS Smart Home",
            model=config.get(CONF_MODEL) or device_data.get("manufacturer"),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._hub.async_add_listener(self._async_hub_updated))

    @callback
    def _async_hub_updated(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._hub.available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "broadlink_host": self._hub.host,
            "broadlink_status": self._hub.status,
        }

    # ---- sending ---------------------------------------------------------
    async def async_send_with_rollback(
        self,
        command: str | list[str] | None,
        snapshot: dict[str, Any],
        *,
        coalesce_key: str | None = None,
    ) -> bool:
        """Show the new state at once, put it back if the command was lost.

        The caller has already applied the optimistic state to the entity; this
        writes it, sends, and restores `snapshot` if the device never took it.
        """
        self.async_write_ha_state()

        if command is None:
            _LOGGER.warning("%s: no IR code for the requested state", self._attr_name)
            self._restore(snapshot)
            return False

        payload = command if isinstance(command, list) else [command]
        encoding = self._data.get("commandsEncoding", "Base64")
        delivered = True
        for index, part in enumerate(payload):
            frame = _decode(part, encoding)
            if frame is None:
                delivered = False
                break
            # Only a state-carrying command may be collapsed with its own
            # repeats, and only the first frame of a multi-frame command.
            key = coalesce_key if (coalesce_key and index == 0) else None
            delivered = await self._hub.async_send(frame, coalesce_key=key)
            if not delivered:
                break

        if not delivered:
            self._restore(snapshot)
        return delivered

    @callback
    def _restore(self, snapshot: dict[str, Any]) -> None:
        for attribute, value in snapshot.items():
            setattr(self, attribute, value)
        self.async_write_ha_state()


def _decode(command: str, encoding: str) -> bytes | None:
    """Turn a stored command into the bytes a Broadlink expects."""
    try:
        if encoding == "Hex":
            return binascii.unhexlify(command)
        if encoding == "Raw":
            return _durations_to_frame(_parse_raw(command))
        if encoding == "Pronto":
            return _durations_to_frame(_parse_pronto(command))
        return base64.b64decode(command)
    except (ValueError, binascii.Error) as err:
        _LOGGER.error("Unreadable IR command (%s): %s", encoding, err)
        return None


# Broadlink IR timing: one tick is 269/8192 microseconds.
_TICK = 269.0 / 8192.0


def _durations_to_frame(durations: list[int]) -> bytes:
    payload = bytearray()
    for duration in durations:
        ticks = int(round(abs(duration) * _TICK))
        if ticks > 255:
            payload += bytes([0x00, ticks >> 8, ticks & 0xFF])
        else:
            payload += bytes([ticks])

    frame = bytearray([0x26, 0x00])  # 0x26 = IR, no repeat
    length = len(payload) + 2
    frame += bytes([length & 0xFF, length >> 8])
    frame += payload
    frame += bytes([0x0D, 0x05])
    while len(frame) % 16 != 0:
        frame.append(0x00)
    return bytes(frame)


def _parse_raw(command: str) -> list[int]:
    cleaned = command.strip().lstrip("[").rstrip("]")
    return [int(round(float(part))) for part in cleaned.split(",") if part.strip()]


def _parse_pronto(command: str) -> list[int]:
    words = [int(part, 16) for part in command.split()]
    if len(words) < 4 or words[0] != 0:
        raise ValueError("unsupported Pronto code")
    frequency_word = words[1]
    carrier = 1000000.0 / (frequency_word * 0.241246) if frequency_word else 38000.0
    once_len, repeat_len = words[2], words[3]
    burst = words[4:]
    sequence = burst[: 2 * once_len] or burst[2 * once_len : 2 * once_len + 2 * repeat_len]
    period = 1000000.0 / carrier
    return [int(round(count * period)) for count in sequence]
