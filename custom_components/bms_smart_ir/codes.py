"""Where IR code files come from, and where they stay.

A code file is what turns "cool, 23 degrees, medium fan" into a pulse table.
Without it an appliance has no commands at all, so it is treated as user data:
downloaded once when the device is added, then kept in the configuration
directory and never fetched again. Nothing at runtime depends on the network.

Layout (unchanged from the previous release, so existing installations keep
their files):

    <config>/bms_smart_ir_codes/<device_type>/<code>.json

TV codes ship inside the integration, so a television can be added with no
internet at all. Air conditioner files are far too large to bundle (the full
SmartIR set is ~50 MB), so those are fetched once at add time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client

from .const import DEVICE_TYPE_CLIMATE, SMARTIR_RAW_BASE

_LOGGER = logging.getLogger(__package__)

USER_CODES_DIR = "bms_smart_ir_codes"

# Brands seen on our sites go to the top of the picker; the rest stays
# alphabetical. Purely presentational — nothing depends on this list.
POPULAR_BRANDS = (
    "Gree",
    "Midea",
    "Haier",
    "Samsung",
    "LG",
    "Panasonic",
    "Toshiba",
    "Daikin",
    "Hisense",
    "TCL",
    "Ballu",
    "Zanussi",
    "Electrolux",
    "Chigo",
    "AUX",
    "Fujitsu",
    "Hitachi",
    "Carrier",
    "Cooper & Hunter",
    "Roda",
)

# Human labels for the panel. The device files speak English; the people
# using the panel do not.
MODE_LABELS_RU = {
    "off": "Выключено",
    "cool": "Охлаждение",
    "heat": "Обогрев",
    "auto": "Авто",
    "dry": "Осушение",
    "fan_only": "Вентиляция",
    "fan": "Вентиляция",
    "heat_cool": "Авто (тепло/холод)",
}

FAN_LABELS_RU = {
    "auto": "Авто",
    "low": "Низкая",
    "mid": "Средняя",
    "middle": "Средняя",
    "medium": "Средняя",
    "high": "Высокая",
    "highest": "Максимальная",
    "silent": "Тихая",
    "quiet": "Тихая",
    "turbo": "Турбо",
}

SWING_LABELS_RU = {
    "auto": "Авто",
    "off": "Выключено",
    "on": "Включено",
    "swing": "Качание",
    "vertical": "Вертикально",
    "horizontal": "Горизонтально",
    "both": "Обе оси",
    "middle": "Середина",
    "top": "Вверх",
    "bottom": "Вниз",
}

DEVICE_TYPE_LABELS_RU = {
    "climate": "Кондиционер",
    "media_player": "Телевизор",
}


def bundled_dir(device_type: str) -> str:
    return os.path.join(os.path.dirname(__file__), "codes", device_type)


def user_dir(hass: HomeAssistant, device_type: str) -> str:
    return hass.config.path(USER_CODES_DIR, device_type)


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as err:
        _LOGGER.warning("Could not read code file %s: %s", path, err)
        return None


def _write_json(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)  # never leave a half-written code file behind


def _load_sync(hass_config_dir: str, device_type: str, code: str) -> dict | None:
    """Look for a code file on disk, user copy first."""
    for directory in (
        os.path.join(hass_config_dir, USER_CODES_DIR, device_type),
        bundled_dir(device_type),
    ):
        path = os.path.join(directory, f"{code}.json")
        if os.path.exists(path):
            data = _read_json(path)
            if data:
                return data
    return None


async def async_load_code(
    hass: HomeAssistant, device_type: str, code: str, *, allow_download: bool = True
) -> dict | None:
    """Return a code file, fetching it once if it has never been seen.

    `allow_download=False` is what runtime uses: an installation that has been
    working for a year must not start depending on GitHub after a reboot.
    """
    data = await hass.async_add_executor_job(
        _load_sync, hass.config.config_dir, device_type, code
    )
    if data is not None:
        return data
    if not allow_download:
        return None
    return await async_download_code(hass, device_type, code)


async def async_download_code(
    hass: HomeAssistant, device_type: str, code: str
) -> dict | None:
    """Fetch a code from SmartIR and keep it for good."""
    url = f"{SMARTIR_RAW_BASE}/{device_type}/{code}.json"
    try:
        session = aiohttp_client.async_get_clientsession(hass)
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                _LOGGER.error("Download of %s failed: HTTP %s", url, resp.status)
                return None
            text = await resp.text()
        data = json.loads(text)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
        _LOGGER.error("Could not download code %s: %s", code, err)
        return None

    path = os.path.join(user_dir(hass, device_type), f"{code}.json")
    try:
        await hass.async_add_executor_job(_write_json, path, text)
    except OSError as err:
        # Not fatal for this run, but it means the next restart downloads again.
        _LOGGER.warning("Could not store code %s: %s", code, err)
    return data


async def async_have_code(hass: HomeAssistant, device_type: str, code: str) -> bool:
    return (
        await hass.async_add_executor_job(
            _load_sync, hass.config.config_dir, device_type, code
        )
        is not None
    )


# ---- catalogue -----------------------------------------------------------
def _index_path(device_type: str) -> str:
    return os.path.join(os.path.dirname(__file__), "codes", f"{device_type}_index.json")


def _load_index(device_type: str) -> list[dict]:
    data = _read_json(_index_path(device_type))
    return data if isinstance(data, list) else []


def _brand_sort_key(manufacturer: str) -> tuple[int, str]:
    name = manufacturer.strip()
    for position, brand in enumerate(POPULAR_BRANDS):
        if brand.lower() == name.lower():
            return (position, name.lower())
    return (len(POPULAR_BRANDS), name.lower())


def _build_catalog(device_type: str, controller: str | None) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in _load_index(device_type):
        if controller and item.get("controller") != controller:
            continue
        manufacturer = (item.get("manufacturer") or "Неизвестный").strip()
        models = item.get("models") or ["Generic"]
        grouped[manufacturer].append(
            {
                "code": str(item["code"]),
                "model": ", ".join(str(model) for model in models),
            }
        )

    catalog = [
        {
            "manufacturer": manufacturer,
            "popular": _brand_sort_key(manufacturer)[0] < len(POPULAR_BRANDS),
            "models": sorted(models, key=lambda entry: entry["model"].lower()),
        }
        for manufacturer, models in grouped.items()
    ]
    catalog.sort(key=lambda entry: _brand_sort_key(entry["manufacturer"]))
    return catalog


async def async_catalog(
    hass: HomeAssistant, device_type: str, controller: str | None = None
) -> list[dict]:
    """Manufacturers with their models, popular brands first."""
    return await hass.async_add_executor_job(_build_catalog, device_type, controller)


def describe_code(data: dict[str, Any]) -> dict[str, Any]:
    """Summarise a code file for the panel, in Russian."""
    modes = [
        MODE_LABELS_RU.get(mode, mode) for mode in data.get("operationModes", []) or []
    ]
    fans = [
        FAN_LABELS_RU.get(str(fan).lower(), str(fan))
        for fan in data.get("fanModes", []) or []
    ]
    swings = [
        SWING_LABELS_RU.get(str(swing).lower(), str(swing))
        for swing in data.get("swingModes", []) or []
    ]
    return {
        "manufacturer": data.get("manufacturer"),
        "models": data.get("supportedModels") or [],
        "modes": modes,
        "fan_modes": fans,
        "swing_modes": swings,
        "min_temperature": data.get("minTemperature"),
        "max_temperature": data.get("maxTemperature"),
        "commands": len(data.get("commands", {}) or {}),
    }


def representative_command(data: dict) -> str | None:
    """Pick one command that visibly does something, for a live test."""
    commands = data.get("commands", {}) or {}
    modes = data.get("operationModes", []) or []
    fans = data.get("fanModes", []) or []
    prefer_fan = fans[len(fans) // 2] if fans else None

    mode = "cool" if "cool" in modes else (modes[0] if modes else None)
    if mode and mode in commands:
        leaf = _first_leaf(commands[mode], prefer_fan)
        if leaf:
            return leaf

    for key, value in commands.items():
        if key == "off":
            continue
        leaf = _first_leaf(value, prefer_fan)
        if leaf:
            return leaf
    return commands.get("off")


def _first_leaf(node: Any, prefer_key: str | None = None) -> str | None:
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if prefer_key and prefer_key in node:
            leaf = _first_leaf(node[prefer_key])
            if leaf:
                return leaf
        for value in node.values():
            leaf = _first_leaf(value, prefer_key)
            if leaf:
                return leaf
    return None


def test_command(data: dict, device_type: str) -> str | None:
    """A command safe to fire while the installer watches the appliance."""
    if device_type == DEVICE_TYPE_CLIMATE:
        return representative_command(data)
    commands = data.get("commands", {}) or {}
    for key in ("volumeUp", "mute", "on", "off"):
        if isinstance(commands.get(key), str):
            return commands[key]
    return _first_leaf(commands)
