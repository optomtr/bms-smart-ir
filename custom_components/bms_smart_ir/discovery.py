"""Finding emitters and poking them once.

Used by the config flow, the panel and the tests. Every call is one-shot: it
opens nothing that has to be closed and keeps no state, so it can be used while
a hub for the same address is running without disturbing its session.
"""

from __future__ import annotations

import logging
from typing import Any

import broadlink

from homeassistant.core import HomeAssistant

from .const import DEFAULT_PORT, DEFAULT_TIMEOUT

_LOGGER = logging.getLogger(__package__)


def describe_device(device: Any) -> dict[str, Any]:
    return {
        "host": device.host[0],
        "port": device.host[1],
        "mac": ":".join(f"{b:02X}" for b in device.mac),
        "model": device.model or device.type,
        "name": device.name or "",
        "devtype": device.devtype,
    }


def _discover_sync(timeout: int, port: int) -> list[dict[str, Any]]:
    return [
        describe_device(device)
        for device in broadlink.discover(timeout=timeout, discover_ip_port=port)
    ]


def _probe_sync(host: str, port: int, timeout: int) -> dict[str, Any] | None:
    try:
        return describe_device(broadlink.hello(host, port, timeout=timeout))
    except Exception as err:  # noqa: BLE001 - "nobody there" is a normal answer
        _LOGGER.debug("No Broadlink at %s:%s (%s)", host, port, err)
        return None


def _send_sync(host: str, port: int, timeout: int, frame: bytes) -> bool:
    """Connect, transmit once, forget. For tests during setup only."""
    try:
        device = broadlink.hello(host, port, timeout=timeout)
        device.timeout = timeout
        device.auth()
        device.send_data(frame)
        return True
    except Exception as err:  # noqa: BLE001 - reported to the installer as-is
        _LOGGER.warning("Test transmission to %s:%s failed: %s", host, port, err)
        return False


async def async_discover(
    hass: HomeAssistant, *, timeout: int = 4, port: int = DEFAULT_PORT
) -> list[dict[str, Any]]:
    """Broadcast and collect whoever answers."""
    return await hass.async_add_executor_job(_discover_sync, timeout, port)


async def async_probe(
    hass: HomeAssistant,
    host: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any] | None:
    """Ask one address directly — works where broadcast is blocked."""
    return await hass.async_add_executor_job(_probe_sync, host, port, timeout)


async def async_send_test(
    hass: HomeAssistant,
    host: str,
    frame: bytes,
    *,
    port: int = DEFAULT_PORT,
    timeout: int = DEFAULT_TIMEOUT,
) -> bool:
    return await hass.async_add_executor_job(_send_sync, host, port, timeout, frame)


def split_host(host: str, port: int = DEFAULT_PORT) -> tuple[str, int]:
    """Accept '192.168.1.50' and '192.168.1.50:80' alike."""
    host = host.strip()
    if host.count(":") == 1:
        address, _, text = host.partition(":")
        if text.isdigit():
            return address.strip(), int(text)
    return host, port
