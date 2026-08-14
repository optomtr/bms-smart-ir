"""The Broadlink itself as a device in Home Assistant.

Before this, only appliances existed and the emitter was an IP address hidden
in a config entry — so nothing could show which air conditioners hang off which
box, and replacing a dead emitter meant recreating every appliance on it.

The device is keyed by address rather than MAC on purpose: the address is known
before the first connection, so the card and its history exist even while the
box is offline. Moving a box to a new address renames the device (see
`async_move_hub_device`) instead of leaving an orphan behind.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    BACKEND_BROADLINK,
    CONF_BACKEND,
    CONF_HOST,
    CONF_PORT,
    DEFAULT_PORT,
    DOMAIN,
)
from .hub import BroadlinkHub, hub_key

MANUFACTURER = "Broadlink"


def hub_identifier(host: str, port: int = DEFAULT_PORT) -> tuple[str, str]:
    return (DOMAIN, f"hub:{hub_key(host, port)}")


def hub_device_info(hub: BroadlinkHub) -> DeviceInfo:
    info = DeviceInfo(
        identifiers={hub_identifier(hub.host, hub.port)},
        manufacturer=MANUFACTURER,
        model=hub.model or "Broadlink",
        # The key, not the bare address: two emitters that differ only by port
        # (a test bench, or a device behind a port mapping) must not end up with
        # the same name and an unreadable list of entities.
        name=hub.device_name or f"Broadlink {hub.hub_id}",
        configuration_url=f"http://{hub.host}",
    )
    if hub.mac:
        info["connections"] = {(dr.CONNECTION_NETWORK_MAC, dr.format_mac(hub.mac_text))}
    return info


def entries_for_hub(
    hass: HomeAssistant, host: str, port: int = DEFAULT_PORT
) -> list[ConfigEntry]:
    """Every appliance configured behind one emitter."""
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_BACKEND) == BACKEND_BROADLINK
        and entry.data.get(CONF_HOST) == host
        and entry.data.get(CONF_PORT, DEFAULT_PORT) == port
    ]


def is_hub_owner(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """True for the one entry that owns the emitter's own entities.

    Deterministic (lowest entry id) rather than first-come, so the owner is the
    same after every restart and the sensors keep their history.
    """
    host = entry.data.get(CONF_HOST)
    if not host:
        return False
    siblings = entries_for_hub(hass, host, entry.data.get(CONF_PORT, DEFAULT_PORT))
    return bool(siblings) and min(item.entry_id for item in siblings) == entry.entry_id


@callback
def async_register_hub_device(
    hass: HomeAssistant, entry: ConfigEntry, hub: BroadlinkHub
) -> None:
    """Create/refresh the emitter's device card and hang the appliance off it."""
    registry = dr.async_get(hass)
    hub_device = registry.async_get_or_create(
        config_entry_id=entry.entry_id, **hub_device_info(hub)
    )

    appliance = registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    if appliance is not None and appliance.via_device_id != hub_device.id:
        registry.async_update_device(appliance.id, via_device_id=hub_device.id)


@callback
def async_move_hub_device(
    hass: HomeAssistant,
    old_host: str,
    new_host: str,
    old_port: int = DEFAULT_PORT,
    new_port: int = DEFAULT_PORT,
) -> None:
    """Follow an emitter that changed address, keeping its history."""
    old = hub_identifier(old_host, old_port)
    new = hub_identifier(new_host, new_port)
    if old == new:
        return
    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={old})
    if device is None:
        return
    identifiers = {item for item in device.identifiers if item != old}
    identifiers.add(new)
    registry.async_update_device(
        device.id,
        new_identifiers=identifiers,
        configuration_url=f"http://{new_host}",
    )
