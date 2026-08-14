"""Everything the panel is allowed to ask for.

The panel reads nothing directly — no runtime objects, no files, no registry.
It sends these commands and renders what comes back, which keeps one place to
audit for what leaves the server and one place to enforce who may change what.

Anything that changes the installation requires an administrator. Checking that
in the interface only would leave the command itself open to any logged-in user.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .codes import (
    DEVICE_TYPE_LABELS_RU,
    async_catalog,
    async_load_code,
    describe_code,
    test_command,
)
from .const import (
    BACKEND_BROADLINK,
    CONF_AREA,
    CONF_BACKEND,
    CONF_DEVICE_CODE,
    CONF_DEVICE_TYPE,
    CONF_HOST,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_NAME,
    CONF_PORT,
    CONTROLLER_BROADLINK,
    DEFAULT_PORT,
    DEVICE_TYPE_CLIMATE,
    DEVICE_TYPE_MEDIA_PLAYER,
    DOMAIN,
)
from .discovery import async_discover, async_probe, split_host
from .entity import _decode
from .hub import async_hubs, hub_key
from .hub_device import async_move_hub_device, entries_for_hub

_LOGGER = logging.getLogger(__package__)

DATA_WS_REGISTERED = "websocket_registered"


@callback
def async_register_websocket_api(hass: HomeAssistant) -> None:
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_WS_REGISTERED):
        return
    domain_data[DATA_WS_REGISTERED] = True
    for command in (
        ws_overview,
        ws_hub,
        ws_catalog,
        ws_areas,
        ws_history,
        ws_discover,
        ws_probe,
        ws_test_code,
        ws_add_appliance,
        ws_update_appliance,
        ws_remove_appliance,
        ws_replace_hub,
    ):
        websocket_api.async_register_command(hass, command)


# ---- reading -------------------------------------------------------------
def _appliance_entity(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """The entity an appliance entry publishes, with its current state."""
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.unique_id != entry.entry_id:
            continue  # the emitter's own sensors, not the appliance
        state = hass.states.get(entity.entity_id)
        return {
            "entity_id": entity.entity_id,
            "state": state.state if state else None,
            "attributes": dict(state.attributes) if state else {},
        }
    return {"entity_id": None, "state": None, "attributes": {}}


def _appliances(
    hass: HomeAssistant, host: str, port: int = DEFAULT_PORT
) -> list[dict[str, Any]]:
    registry = dr.async_get(hass)
    result = []
    for entry in entries_for_hub(hass, host, port):
        config = {**entry.data, **entry.options}
        device = registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
        device_type = config.get(CONF_DEVICE_TYPE, DEVICE_TYPE_CLIMATE)
        result.append(
            {
                "entry_id": entry.entry_id,
                "name": config.get(CONF_NAME) or entry.title,
                "device_type": device_type,
                "device_type_label": DEVICE_TYPE_LABELS_RU.get(device_type, device_type),
                "manufacturer": config.get(CONF_MANUFACTURER),
                "model": config.get(CONF_MODEL),
                "code": config.get(CONF_DEVICE_CODE),
                "area_id": device.area_id if device else config.get(CONF_AREA),
                **_appliance_entity(hass, entry),
            }
        )
    return sorted(result, key=lambda item: (item["device_type"], item["name"].lower()))


def _hub_entities(hass: HomeAssistant, host: str, port: int) -> dict[str, str]:
    """The emitter's own entities (its thermometer and its link sensor).

    Looked up here rather than guessed in the panel by name: a renamed device
    would break name matching, and the panel must not have to know how unique
    ids are built.
    """
    registry = er.async_get(hass)
    found: dict[str, str] = {}
    for entry in entries_for_hub(hass, host, port):
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            for suffix in ("temperature", "humidity", "online"):
                if entity.unique_id.endswith(f"_{suffix}"):
                    found[suffix] = entity.entity_id
    return found


def _hub_payload(hass: HomeAssistant, hub) -> dict[str, Any]:
    payload = hub.describe()
    payload["appliances"] = _appliances(hass, hub.host, hub.port)
    payload["entities"] = _hub_entities(hass, hub.host, hub.port)
    return payload


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/overview"})
@callback
def ws_overview(hass: HomeAssistant, connection, msg: dict) -> None:
    """Everything the first screen needs, in one round trip."""
    hubs = [_hub_payload(hass, hub) for hub in async_hubs(hass).values()]
    hubs.sort(key=lambda item: item["host"])
    connection.send_result(
        msg["id"],
        {
            "hubs": hubs,
            "totals": {
                "hubs": len(hubs),
                "online": sum(1 for hub in hubs if hub["status"] == "online"),
                "appliances": sum(len(hub["appliances"]) for hub in hubs),
                "with_sensor": sum(1 for hub in hubs if hub.get("has_sensor")),
            },
        },
    )


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/hub", vol.Required("host"): str}
)
@callback
def ws_hub(hass: HomeAssistant, connection, msg: dict) -> None:
    hub = async_hubs(hass).get(msg["host"])
    if hub is None:
        connection.send_error(msg["id"], "not_found", "Устройство не найдено")
        return
    payload = _hub_payload(hass, hub)
    payload["uptime_log"] = [
        {"at": at, "online": online} for at, online in hub.uptime_log
    ]
    connection.send_result(msg["id"], payload)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/catalog",
        vol.Required("device_type"): vol.In([DEVICE_TYPE_CLIMATE, DEVICE_TYPE_MEDIA_PLAYER]),
    }
)
@websocket_api.async_response
async def ws_catalog(hass: HomeAssistant, connection, msg: dict) -> None:
    catalog = await async_catalog(hass, msg["device_type"], CONTROLLER_BROADLINK)
    connection.send_result(msg["id"], {"catalog": catalog})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/areas"})
@callback
def ws_areas(hass: HomeAssistant, connection, msg: dict) -> None:
    registry = ar.async_get(hass)
    areas = [{"area_id": area.id, "name": area.name} for area in registry.async_list_areas()]
    areas.sort(key=lambda item: item["name"].lower())
    connection.send_result(msg["id"], {"areas": areas})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/history",
        vol.Required("entity_ids"): [str],
        vol.Optional("hours", default=24): vol.All(int, vol.Range(min=1, max=168)),
    }
)
@websocket_api.async_response
async def ws_history(hass: HomeAssistant, connection, msg: dict) -> None:
    """Recorded history for the charts. Empty when the recorder is disabled."""
    try:
        from homeassistant.components.recorder import get_instance, history
    except ImportError:
        connection.send_result(msg["id"], {"history": {}})
        return

    end = dt_util.utcnow()
    start = end - timedelta(hours=msg["hours"])
    states = await get_instance(hass).async_add_executor_job(
        history.get_significant_states,
        hass,
        start,
        end,
        msg["entity_ids"],
        None,
        True,
        True,
    )
    payload = {
        entity_id: [
            {"at": state.last_changed.timestamp(), "state": state.state}
            for state in entity_states
        ]
        for entity_id, entity_states in (states or {}).items()
    }
    connection.send_result(msg["id"], {"history": payload})


# ---- discovery -----------------------------------------------------------
def _mark_known(hass: HomeAssistant, found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    configured = {
        hub_key(entry.data.get(CONF_HOST), entry.data.get(CONF_PORT, DEFAULT_PORT))
        for entry in _broadlink_entries(hass)
    }
    for device in found:
        device["known"] = hub_key(device["host"], device["port"]) in configured
        device["appliances"] = len(_appliances(hass, device["host"], device["port"]))
    return found


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/discover",
        vol.Optional("timeout", default=4): vol.All(int, vol.Range(min=1, max=15)),
        vol.Optional("port", default=DEFAULT_PORT): int,
    }
)
@websocket_api.async_response
async def ws_discover(hass: HomeAssistant, connection, msg: dict) -> None:
    """Find emitters on the network that answer a broadcast."""
    found = await async_discover(hass, timeout=msg["timeout"], port=msg["port"])
    connection.send_result(msg["id"], {"devices": _mark_known(hass, found)})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/probe",
        vol.Required("host"): str,
        vol.Optional("port", default=DEFAULT_PORT): int,
        vol.Optional("timeout", default=4): vol.All(int, vol.Range(min=1, max=15)),
    }
)
@websocket_api.async_response
async def ws_probe(hass: HomeAssistant, connection, msg: dict) -> None:
    """Ask one address directly — networks that block broadcast still work."""
    host, port = split_host(msg["host"], msg["port"])
    device = await async_probe(hass, host, port=port, timeout=msg["timeout"])
    if device is None:
        connection.send_error(
            msg["id"], "not_found", f"По адресу {host}:{port} никто не ответил"
        )
        return
    connection.send_result(msg["id"], {"devices": _mark_known(hass, [device])})


# ---- changing the installation -------------------------------------------
@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/test_code",
        vol.Required("host"): str,
        vol.Required("device_type"): vol.In(
            [DEVICE_TYPE_CLIMATE, DEVICE_TYPE_MEDIA_PLAYER]
        ),
        vol.Required("code"): str,
        vol.Optional("port", default=DEFAULT_PORT): int,
    }
)
@websocket_api.async_response
async def ws_test_code(hass: HomeAssistant, connection, msg: dict) -> None:
    """Fire one signal so the installer can see whether the code is right."""
    data = await async_load_code(hass, msg["device_type"], msg["code"])
    if not data:
        connection.send_error(msg["id"], "no_code", "Код не найден и не скачался")
        return

    command = test_command(data, msg["device_type"])
    frame = _decode(command, data.get("commandsEncoding", "Base64")) if command else None
    if frame is None:
        connection.send_error(msg["id"], "no_command", "В этом коде нечего отправить")
        return

    host, port = split_host(msg["host"], msg["port"])
    hub = async_hubs(hass).get(hub_key(host, port))
    if hub is None:
        from .hub import BroadlinkHub

        hub = BroadlinkHub(hass, host, port=port)
        await hub.async_start(with_heartbeat=False)
        try:
            delivered = await hub.async_send(frame)
        finally:
            await hub.async_stop()
    else:
        delivered = await hub.async_send(frame)

    connection.send_result(
        msg["id"], {"delivered": delivered, "code": describe_code(data)}
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/add_appliance",
        vol.Required("host"): str,
        vol.Required("device_type"): vol.In(
            [DEVICE_TYPE_CLIMATE, DEVICE_TYPE_MEDIA_PLAYER]
        ),
        vol.Required("code"): str,
        vol.Required("name"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Optional("manufacturer"): vol.Any(str, None),
        vol.Optional("model"): vol.Any(str, None),
        vol.Optional("area_id"): vol.Any(str, None),
        vol.Optional("port", default=DEFAULT_PORT): int,
    }
)
@websocket_api.async_response
async def ws_add_appliance(hass: HomeAssistant, connection, msg: dict) -> None:
    host, port = split_host(msg["host"], msg["port"])
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "import"},
        data={
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_DEVICE_TYPE: msg["device_type"],
            CONF_DEVICE_CODE: msg["code"],
            CONF_NAME: msg["name"],
            CONF_MANUFACTURER: msg.get("manufacturer"),
            CONF_MODEL: msg.get("model"),
            CONF_AREA: msg.get("area_id"),
        },
    )
    if result.get("type") != "create_entry":
        connection.send_error(
            msg["id"], "add_failed", str(result.get("reason") or "Не удалось добавить")
        )
        return
    connection.send_result(msg["id"], {"entry_id": result["result"].entry_id})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/update_appliance",
        vol.Required("entry_id"): str,
        vol.Optional("name"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Optional("area_id"): vol.Any(str, None),
        vol.Optional("code"): str,
    }
)
@websocket_api.async_response
async def ws_update_appliance(hass: HomeAssistant, connection, msg: dict) -> None:
    """Rename, move to another room, or point at a different code."""
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if entry is None or entry.data.get(CONF_BACKEND) != BACKEND_BROADLINK:
        connection.send_error(msg["id"], "not_found", "Устройство не найдено")
        return

    data = {**entry.data}
    if "name" in msg:
        data[CONF_NAME] = msg["name"]
    if "code" in msg:
        data[CONF_DEVICE_CODE] = msg["code"]
    if "area_id" in msg:
        data[CONF_AREA] = msg["area_id"]
        registry = dr.async_get(hass)
        device = registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
        if device is not None:
            registry.async_update_device(device.id, area_id=msg["area_id"])

    hass.config_entries.async_update_entry(
        entry, data=data, title=data.get(CONF_NAME, entry.title)
    )
    await hass.config_entries.async_reload(entry.entry_id)
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/remove_appliance", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_remove_appliance(hass: HomeAssistant, connection, msg: dict) -> None:
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if entry is None:
        connection.send_error(msg["id"], "not_found", "Устройство не найдено")
        return
    await hass.config_entries.async_remove(msg["entry_id"])
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/replace_hub",
        vol.Required("host"): str,
        vol.Required("new_host"): str,
        vol.Optional("new_port", default=DEFAULT_PORT): int,
    }
)
@websocket_api.async_response
async def ws_replace_hub(hass: HomeAssistant, connection, msg: dict) -> None:
    """Point every appliance of one emitter at another box.

    This is the field repair: a dead RM4 is swapped for a new one and all the
    air conditioners in front of it keep their entity ids, their history and
    their automations.
    """
    old_hub = async_hubs(hass).get(msg["host"])
    if old_hub is None:
        connection.send_error(msg["id"], "not_found", "Такого устройства нет")
        return
    old_host, old_port = old_hub.host, old_hub.port
    new_host, new_port = split_host(msg["new_host"], msg["new_port"])
    entries = entries_for_hub(hass, old_host, old_port)
    if not entries:
        connection.send_error(msg["id"], "not_found", "У этого устройства нет приборов")
        return

    device = await async_probe(hass, new_host, port=new_port, timeout=4)
    if device is None:
        connection.send_error(
            msg["id"], "not_found", f"По адресу {new_host}:{new_port} никто не ответил"
        )
        return

    async_move_hub_device(hass, old_host, new_host, old_port, new_port)
    for entry in entries:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_HOST: new_host, CONF_PORT: new_port}
        )
    for entry in entries:
        await hass.config_entries.async_reload(entry.entry_id)

    connection.send_result(msg["id"], {"moved": len(entries), "device": device})


# ---- helpers -------------------------------------------------------------
def _broadlink_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_BACKEND) == BACKEND_BROADLINK
    ]


