"""Following a Broadlink across a DHCP lease change.

The IP address is not a Broadlink's identity — the MAC is, and a router never
hands out a new one of those. When a hub has been unreachable at its known
address for a while (see `hub.wants_mac_rediscovery`), `async_follow_mac`
broadcasts a discovery scan and asks whether that MAC answered somewhere else.

If it did, every appliance behind the hub is moved to the new address without
touching a single entity: they already read the emitter's address off the live
hub object (see `entity.py`'s `extra_state_attributes` and the hub's own
`describe()`), not out of stored config, so relocating the hub in place is
enough to make control work again immediately. Only the stored configuration
and the device registry card are updated to match — silently, so a Home
Assistant restart still finds the box without flashing anything unavailable
right now.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .const import CONF_HOST, CONF_PORT, DEFAULT_PORT
from .discovery import async_discover
from .hub import BroadlinkHub, async_hubs, async_mark_silent_reload, hub_key
from .hub_device import async_move_hub_device, entries_for_hub

_LOGGER = logging.getLogger(__package__)


async def async_follow_mac(hass: HomeAssistant, hub: BroadlinkHub) -> bool:
    """Look for `hub`'s MAC elsewhere on the network; move it there if found.

    Returns True if the hub was relocated. Real hardware always answers on
    port 80 regardless of what port the hub itself is configured for (a
    nonstandard port only exists on the test bench, where many virtual
    devices share one address), so the broadcast always targets the standard
    port — matching what the panel's own discovery screen already assumes.
    """
    if hub.mac_text is None:
        return False

    old_host, old_port = hub.host, hub.port
    found = await async_discover(hass, timeout=4, port=DEFAULT_PORT)
    match = next((device for device in found if device["mac"] == hub.mac_text), None)
    if match is None:
        return False

    new_host, new_port = match["host"], match["port"]
    if (new_host, new_port) == (old_host, old_port):
        # It answered again at the same place — a slow Wi-Fi reconnect, not a
        # moved box. The ordinary backoff/watchdog cycle already handles that.
        return False

    entries = entries_for_hub(hass, old_host, old_port)
    _LOGGER.warning(
        "%s: тот же MAC (%s) отвечает по новому адресу %s:%s — переезжаю (%d прибор(ов))",
        hub_key(old_host, old_port),
        hub.mac_text,
        new_host,
        new_port,
        len(entries),
    )

    # Move the live object first: every entity already reads its address off
    # it, so this alone restores control before a single byte is persisted.
    hubs = async_hubs(hass)
    hubs.pop(hub_key(old_host, old_port), None)
    hub.host, hub.port = new_host, new_port
    hubs[hub_key(new_host, new_port)] = hub

    async_move_hub_device(hass, old_host, new_host, old_port, new_port)
    for entry in entries:
        async_mark_silent_reload(hass, entry.entry_id)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_HOST: new_host, CONF_PORT: new_port}
        )

    await hub.async_force_reconnect()
    return True
