"""Unified config flow for BMS Smart IR.

Step 1 is a menu: choose the backend.
  * Broadlink -> the SmartIR-style flow (IP, manufacturer, model, test, finish).
  * Tuya      -> the cloud flow (hub id, pick remote, test).
Each branch stores CONF_BACKEND so the rest of the integration knows which
code path to use.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import slugify

from .cloud import TuyaIRCloud
from .const import (
    AC_CATEGORY_IDS,
    AC_NAME_HINTS,
    BACKEND_BROADLINK,
    BACKEND_TUYA,
    CATEGORY_NAMES,
    CONF_AREA,
    CONF_BACKEND,
    CONF_BMS_ENTRY_ID,
    CONF_CATEGORY_ID,
    CONF_CATEGORY_NAME,
    CONF_CONTROLLER,
    CONF_DEVICE_CODE,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_HOST,
    CONF_HUMIDITY_SENSOR,
    CONF_INFRARED_ID,
    CONF_KIND,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_NAME,
    CONF_POWER_SCAN_SENSOR,
    CONF_POWER_SENSOR,
    CONF_PORT,
    CONF_SCAN_THRESHOLD,
    CONF_TEMPERATURE_SENSOR,
    CONF_TIMEOUT,
    CONTROLLER_BROADLINK,
    DEFAULT_MODE_INT,
    DEFAULT_PORT,
    DEFAULT_SCAN_SETTLE,
    DEFAULT_SCAN_THRESHOLD,
    DEFAULT_SCAN_WAIT,
    DEFAULT_TEMP,
    DEFAULT_TIMEOUT,
    DEFAULT_WIND_INT,
    DEVICE_TYPE_CLIMATE,
    DEVICE_TYPE_MEDIA_PLAYER,
    DOMAIN,
    KIND_CLIMATE,
    KIND_REMOTE,
)
from .codes import async_catalog, async_load_code, representative_command
from .discovery import async_probe, async_send_test, split_host
from .entity import _decode
from .helpers import find_bms_creds

_LOGGER = logging.getLogger(__name__)


async def _catalog_map(hass, device_type: str, controller: str) -> dict[str, list[dict]]:
    """The catalogue keyed by manufacturer, which is what the dropdowns want."""
    catalog = await async_catalog(hass, device_type, controller)
    return {entry["manufacturer"]: entry["models"] for entry in catalog}


def appliance_unique_id(data: dict[str, Any]) -> str:
    """Identity of one appliance.

    The name is part of it: two identical air conditioners of the same model on
    one emitter is an ordinary installation, and the previous scheme (type +
    address + code) refused the second one.
    """
    return "_".join(
        [
            data[CONF_DEVICE_TYPE],
            data[CONF_HOST],
            str(data[CONF_DEVICE_CODE]),
            slugify(data[CONF_NAME]),
        ]
    )

RESULT_WORKED = "worked"
RESULT_RESEND = "resend"
RESULT_NEXT = "next"

METHOD_AUTO = "auto"
METHOD_SEQ = "sequential"
METHOD_MANUAL = "manual"

OFF_STATES = ("unknown", "unavailable", "", None)


def _sensors_dict(defaults: dict | None = None) -> dict:
    defaults = defaults or {}

    def _key(name: str):
        if defaults.get(name):
            return vol.Optional(name, default=defaults[name])
        return vol.Optional(name)

    return {
        _key(CONF_TEMPERATURE_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
        ),
        _key(CONF_HUMIDITY_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="humidity")
        ),
        _key(CONF_POWER_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["binary_sensor", "switch", "input_boolean"]
            )
        ),
    }


# ----- Tuya helpers ------------------------------------------------------
def _remote_id(remote: dict) -> str | None:
    return remote.get("remote_id") or remote.get("id") or remote.get("device_id")


def _category_of(remote: dict) -> tuple[str | None, str]:
    cid = remote.get("category_id")
    cid = str(cid) if cid is not None else None
    cname = (
        remote.get("category_name")
        or (CATEGORY_NAMES.get(cid) if cid else None)
        or (f"Category {cid}" if cid else "IR Remote")
    )
    return cid, cname


def _looks_like_ac(cid: str | None, *names: str) -> bool:
    if cid in AC_CATEGORY_IDS:
        return True
    blob = " ".join(str(n).lower() for n in names if n)
    return any(h in blob for h in AC_NAME_HINTS)


def _remote_label(remote: dict) -> str:
    rid = _remote_id(remote) or "?"
    name = remote.get("remote_name") or remote.get("name") or rid
    _, cname = _category_of(remote)
    return f"{name} — {cname}"


class BmsSmartIRConfigFlow(ConfigFlow, domain=DOMAIN):
    """Unified configuration flow."""

    VERSION = 2

    def __init__(self) -> None:
        # Broadlink branch state
        self._data: dict[str, Any] = {}
        self._catalog: dict[str, list[dict]] = {}
        self._manufacturer: str = ""
        self._failed: set[str] = set()
        self._current_code: str = ""
        self._current_model: str = ""
        self._current_data: dict | None = None
        self._working_code: str = ""
        self._test_status: str = ""
        self._scan_mode: bool = False
        self._scan_list: list[str] = []
        self._scan_pos: int = 0
        # Tuya branch state
        self._infrared_id: str = ""
        self._bms_entry_id: str = ""
        self._remotes: list[dict] = []
        self._device_id: str = ""
        self._name: str = ""
        self._kind: str = KIND_REMOTE
        self._category_id: str | None = None
        self._category_name: str = "IR Remote"

    # ===== Step 0: choose backend =======================================
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="user", menu_options=[BACKEND_BROADLINK, BACKEND_TUYA]
        )

    async def async_step_import(
        self, import_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Add an appliance chosen in the panel.

        The panel has already picked the emitter, the code and the name and has
        fired a test signal; this validates and stores the result. Going
        through a flow rather than writing an entry directly keeps one place
        where entries are created.
        """
        data: dict[str, Any] = {
            CONF_BACKEND: BACKEND_BROADLINK,
            CONF_CONTROLLER: CONTROLLER_BROADLINK,
            CONF_NAME: import_data[CONF_NAME].strip(),
            CONF_HOST: import_data[CONF_HOST],
            CONF_PORT: import_data.get(CONF_PORT, DEFAULT_PORT),
            CONF_TIMEOUT: DEFAULT_TIMEOUT,
            CONF_DEVICE_TYPE: import_data[CONF_DEVICE_TYPE],
            CONF_DEVICE_CODE: str(import_data[CONF_DEVICE_CODE]),
        }
        for optional in (CONF_MANUFACTURER, CONF_MODEL, CONF_AREA):
            if import_data.get(optional):
                data[optional] = import_data[optional]

        if not await async_load_code(
            self.hass, data[CONF_DEVICE_TYPE], data[CONF_DEVICE_CODE]
        ):
            return self.async_abort(reason="no_codes")

        await self.async_set_unique_id(appliance_unique_id(data))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=data[CONF_NAME], data=data)

    # ====================================================================
    # BROADLINK BRANCH
    # ====================================================================
    @property
    def _dir(self) -> str:
        return os.path.dirname(__file__)

    def _candidates(self) -> list[dict]:
        return self._catalog.get(self._manufacturer, [])

    async def _send(self, command: str, data: dict) -> bool:
        """Fire one command at the emitter being configured."""
        frame = _decode(command, data.get("commandsEncoding", "Base64"))
        if frame is None:
            return False
        return await async_send_test(
            self.hass,
            self._data[CONF_HOST],
            frame,
            port=self._data.get(CONF_PORT, DEFAULT_PORT),
            timeout=self._data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
        )

    async def async_step_broadlink(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host, port = split_host(user_input[CONF_HOST])
            timeout = int(user_input.get(CONF_TIMEOUT, DEFAULT_TIMEOUT))
            if await async_probe(self.hass, host, port=port, timeout=timeout):
                self._data[CONF_BACKEND] = BACKEND_BROADLINK
                self._data[CONF_NAME] = user_input[CONF_NAME]
                self._data[CONF_HOST] = host
                self._data[CONF_PORT] = port
                self._data[CONF_TIMEOUT] = timeout
                self._data[CONF_CONTROLLER] = CONTROLLER_BROADLINK
                return await self.async_step_bl_type()
            errors["base"] = "cannot_connect"

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): selector.TextSelector(),
                vol.Required(CONF_HOST): selector.TextSelector(),
                vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=30, mode=selector.NumberSelectorMode.BOX
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="broadlink", data_schema=schema, errors=errors
        )

    async def async_step_bl_type(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose what kind of device to add over Broadlink: AC or TV."""
        if user_input is not None:
            self._catalog = {}
            self._manufacturer = ""
            self._failed = set()
            if user_input["device_type"] == DEVICE_TYPE_MEDIA_PLAYER:
                self._data[CONF_DEVICE_TYPE] = DEVICE_TYPE_MEDIA_PLAYER
                return await self.async_step_tv_manufacturer()
            self._data[CONF_DEVICE_TYPE] = DEVICE_TYPE_CLIMATE
            return await self.async_step_manufacturer()

        options = [
            selector.SelectOptionDict(
                value=DEVICE_TYPE_CLIMATE, label="❄️ Кондиционер"
            ),
            selector.SelectOptionDict(
                value=DEVICE_TYPE_MEDIA_PLAYER, label="📺 Телевизор"
            ),
        ]
        schema = vol.Schema(
            {
                vol.Required(
                    "device_type", default=DEVICE_TYPE_CLIMATE
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options, mode=selector.SelectSelectorMode.LIST
                    )
                )
            }
        )
        return self.async_show_form(step_id="bl_type", data_schema=schema)

    async def async_step_manufacturer(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self._catalog:
            self._catalog = await _catalog_map(
                self.hass, self._data[CONF_DEVICE_TYPE], self._data[CONF_CONTROLLER]
            )
        if not self._catalog:
            return self.async_abort(reason="no_codes")

        if user_input is not None:
            self._manufacturer = user_input[CONF_MANUFACTURER]
            self._data[CONF_MANUFACTURER] = self._manufacturer
            self._failed = set()
            return await self.async_step_method()

        options = [
            selector.SelectOptionDict(value=name, label=name) for name in self._catalog
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_MANUFACTURER): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options, mode=selector.SelectSelectorMode.DROPDOWN
                    )
                )
            }
        )
        return self.async_show_form(step_id="manufacturer", data_schema=schema)

    async def async_step_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            method = user_input["method"]
            if method == METHOD_AUTO:
                return await self.async_step_autodetect()
            if method == METHOD_SEQ:
                return await self.async_step_sequential()
            return await self.async_step_model()

        options = [
            selector.SelectOptionDict(
                value=METHOD_AUTO,
                label="🔍 Avtomatik skan (quvvat sensori bilan, qo'lsiz)",
            ),
            selector.SelectOptionDict(
                value=METHOD_SEQ,
                label="👁️ Ketma-ket skan (kodlar birma-bir yuboriladi, men kuzataman)",
            ),
            selector.SelectOptionDict(
                value=METHOD_MANUAL, label="📝 Bitta kodni qo'lda tanlash"
            ),
        ]
        schema = vol.Schema(
            {
                vol.Required("method", default=METHOD_SEQ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options, mode=selector.SelectSelectorMode.LIST
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="method",
            data_schema=schema,
            description_placeholders={
                "manufacturer": self._manufacturer,
                "count": str(len(self._candidates())),
            },
        )

    async def async_step_model(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._scan_mode = False
        candidates = [e for e in self._candidates() if e["code"] not in self._failed]
        if not candidates:
            return self.async_abort(reason="no_more_codes")

        errors: dict[str, str] = {}
        if user_input is not None:
            code = user_input[CONF_DEVICE_CODE]
            if await self._load_candidate(code):
                return await self.async_step_test()
            errors["base"] = "download_failed"

        options = [
            selector.SelectOptionDict(
                value=e["code"], label=f"{e['model']}  (#{e['code']})"
            )
            for e in candidates
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_CODE): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options, mode=selector.SelectSelectorMode.DROPDOWN
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="model",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "manufacturer": self._manufacturer,
                "remaining": str(len(candidates)),
            },
        )

    async def async_step_sequential(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._scan_mode = True
        self._scan_list = [e["code"] for e in self._candidates()]
        self._scan_pos = 0
        return await self._scan_load_and_test()

    async def _scan_load_and_test(self) -> ConfigFlowResult:
        while self._scan_pos < len(self._scan_list):
            code = self._scan_list[self._scan_pos]
            if await self._load_candidate(code):
                return await self._show_test(send=True)
            self._scan_pos += 1
        return self.async_abort(reason="no_more_codes")

    async def async_step_autodetect(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            sensor = user_input[CONF_POWER_SCAN_SENSOR]
            threshold = float(user_input.get(CONF_SCAN_THRESHOLD, DEFAULT_SCAN_THRESHOLD))
            if await self._run_power_scan(self._candidates(), sensor, threshold):
                return await self.async_step_finish()
            errors["base"] = "scan_no_match"

        schema = vol.Schema(
            {
                vol.Required(CONF_POWER_SCAN_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="power")
                ),
                vol.Optional(
                    CONF_SCAN_THRESHOLD, default=DEFAULT_SCAN_THRESHOLD
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5, max=3000, mode=selector.NumberSelectorMode.BOX
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="autodetect",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "manufacturer": self._manufacturer,
                "count": str(len(self._candidates())),
            },
        )

    def _read_power(self, sensor: str) -> float | None:
        state = self.hass.states.get(sensor)
        if not state or state.state in OFF_STATES:
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    async def _run_power_scan(
        self, candidates: list[dict], sensor: str, threshold: float
    ) -> bool:
        for entry in candidates:
            code = entry["code"]
            data = await async_load_code(self.hass, DEVICE_TYPE_CLIMATE, code)
            if not data:
                continue
            on_command = representative_command(data)
            if not on_command:
                continue
            off_command = data.get("commands", {}).get("off")
            if off_command and not await self._send(off_command, data):
                continue
            await asyncio.sleep(DEFAULT_SCAN_SETTLE)
            baseline = self._read_power(sensor) or 0.0
            if not await self._send(on_command, data):
                continue
            await asyncio.sleep(DEFAULT_SCAN_WAIT)
            after = self._read_power(sensor)

            if after is not None and (after - baseline) >= threshold:
                _LOGGER.info(
                    "BMS Smart IR scan: match on code %s (%.0f -> %.0f W)",
                    code,
                    baseline,
                    after,
                )
                self._current_code = code
                self._current_model = entry["model"]
                self._current_data = data
                self._working_code = code
                return True
        return False

    async def _load_candidate(self, code: str) -> bool:
        self._current_code = code
        self._current_model = next(
            (e["model"] for e in self._candidates() if e["code"] == code),
            self._manufacturer,
        )
        self._current_data = await async_load_code(
            self.hass,
            self._data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_CLIMATE),
            code,
        )
        return self._current_data is not None

    async def async_step_test(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            result = user_input["result"]
            if result == RESULT_WORKED:
                self._working_code = self._current_code
                return await self.async_step_finish()
            if result == RESULT_RESEND:
                return await self._show_test(send=True)
            if self._scan_mode:
                self._scan_pos += 1
                return await self._scan_load_and_test()
            self._failed.add(self._current_code)
            return await self.async_step_model()

        return await self._show_test(send=True)

    async def _show_test(self, send: bool) -> ConfigFlowResult:
        if send:
            await self._send_test()

        progress = ""
        if self._scan_mode and self._scan_list:
            progress = f"🔎 Skan: {self._scan_pos + 1}/{len(self._scan_list)}  "

        options = [
            selector.SelectOptionDict(
                value=RESULT_WORKED, label="✅ Ha, konditsioner javob berdi (saqlash)"
            ),
            selector.SelectOptionDict(
                value=RESULT_RESEND, label="🔁 Signalni qayta yuborish"
            ),
            selector.SelectOptionDict(
                value=RESULT_NEXT, label="➡️ Yo'q, keyingi kodni sinash"
            ),
        ]
        schema = vol.Schema(
            {
                vol.Required("result", default=RESULT_WORKED): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options, mode=selector.SelectSelectorMode.LIST
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="test",
            data_schema=schema,
            description_placeholders={
                "model": self._current_model,
                "code": self._current_code,
                "status": progress + self._test_status,
            },
        )

    async def _send_test(self) -> None:
        if not self._current_data:
            self._test_status = "⚠️ Не удалось прочитать файл кода."
            return
        command = representative_command(self._current_data)
        if not command:
            self._test_status = "⚠️ В этом коде нет команды для отправки."
            return
        if await self._send(command, self._current_data):
            self._test_status = "📡 Тестовый сигнал отправлен."
        else:
            self._test_status = "⚠️ Не удалось отправить — проверьте связь с Broadlink."

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data[CONF_DEVICE_CODE] = self._working_code
            self._data[CONF_MODEL] = self._current_model
            if user_input.get(CONF_AREA):
                self._data[CONF_AREA] = user_input[CONF_AREA]
            for key in (
                CONF_TEMPERATURE_SENSOR,
                CONF_HUMIDITY_SENSOR,
                CONF_POWER_SENSOR,
            ):
                if user_input.get(key):
                    self._data[key] = user_input[key]

            await self.async_set_unique_id(appliance_unique_id(self._data))
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=self._data[CONF_NAME], data=self._data)

        schema = vol.Schema(
            {vol.Optional(CONF_AREA): selector.AreaSelector(), **_sensors_dict()}
        )
        return self.async_show_form(
            step_id="finish",
            data_schema=schema,
            description_placeholders={
                "model": self._current_model,
                "code": self._working_code,
            },
        )

    # ====================================================================
    # BROADLINK TV (media_player) SUB-BRANCH
    # ====================================================================
    async def async_step_tv_manufacturer(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self._catalog:
            self._catalog = await _catalog_map(
                self.hass, DEVICE_TYPE_MEDIA_PLAYER, CONTROLLER_BROADLINK
            )
        if not self._catalog:
            return self.async_abort(reason="no_codes")

        if user_input is not None:
            self._manufacturer = user_input[CONF_MANUFACTURER]
            self._data[CONF_MANUFACTURER] = self._manufacturer
            self._failed = set()
            return await self.async_step_tv_model()

        options = [
            selector.SelectOptionDict(value=name, label=name) for name in self._catalog
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_MANUFACTURER): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options, mode=selector.SelectSelectorMode.DROPDOWN
                    )
                )
            }
        )
        return self.async_show_form(step_id="tv_manufacturer", data_schema=schema)

    async def async_step_tv_model(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        candidates = [e for e in self._candidates() if e["code"] not in self._failed]
        if not candidates:
            return self.async_abort(reason="no_more_codes")

        errors: dict[str, str] = {}
        if user_input is not None:
            code = user_input[CONF_DEVICE_CODE]
            if await self._load_candidate(code):
                return await self.async_step_tv_test()
            errors["base"] = "download_failed"

        options = [
            selector.SelectOptionDict(
                value=e["code"], label=f"{e['model']}  (#{e['code']})"
            )
            for e in candidates
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_CODE): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options, mode=selector.SelectSelectorMode.DROPDOWN
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="tv_model",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "manufacturer": self._manufacturer,
                "remaining": str(len(candidates)),
            },
        )

    async def async_step_tv_test(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            result = user_input["result"]
            if result == RESULT_WORKED:
                self._working_code = self._current_code
                return await self.async_step_tv_finish()
            if result == RESULT_RESEND:
                return await self._show_tv_test(send=True)
            self._failed.add(self._current_code)
            return await self.async_step_tv_model()
        return await self._show_tv_test(send=True)

    async def _show_tv_test(self, send: bool) -> ConfigFlowResult:
        if send:
            await self._send_tv_test()
        options = [
            selector.SelectOptionDict(
                value=RESULT_WORKED, label="✅ Ha, televizor javob berdi (saqlash)"
            ),
            selector.SelectOptionDict(
                value=RESULT_RESEND, label="🔁 Signalni qayta yuborish"
            ),
            selector.SelectOptionDict(
                value=RESULT_NEXT, label="➡️ Yo'q, keyingi kodni sinash"
            ),
        ]
        schema = vol.Schema(
            {
                vol.Required("result", default=RESULT_WORKED): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options, mode=selector.SelectSelectorMode.LIST
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="tv_test",
            data_schema=schema,
            description_placeholders={
                "model": self._current_model,
                "code": self._current_code,
                "status": self._test_status,
            },
        )

    async def _send_tv_test(self) -> None:
        data = self._current_data
        if not data:
            self._test_status = "⚠️ Не удалось прочитать файл кода."
            return
        commands = data.get("commands", {})
        command = (
            commands.get("volumeUp")
            or commands.get("mute")
            or commands.get("on")
            or commands.get("off")
        )
        if not command:
            for value in commands.values():
                if isinstance(value, str):
                    command = value
                    break
        if not command:
            self._test_status = "⚠️ В этом коде нет команды для отправки."
            return
        if await self._send(command, data):
            self._test_status = (
                "📡 Тестовый сигнал отправлен. Телевизор отреагировал "
                "(изменилась громкость, появился значок на экране)?"
            )
        else:
            self._test_status = "⚠️ Не удалось отправить — проверьте связь с Broadlink."

    async def async_step_tv_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data[CONF_DEVICE_CODE] = self._working_code
            self._data[CONF_MODEL] = self._current_model
            if user_input.get(CONF_AREA):
                self._data[CONF_AREA] = user_input[CONF_AREA]
            await self.async_set_unique_id(appliance_unique_id(self._data))
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=self._data[CONF_NAME], data=self._data)

        schema = vol.Schema({vol.Optional(CONF_AREA): selector.AreaSelector()})
        return self.async_show_form(
            step_id="tv_finish",
            data_schema=schema,
            description_placeholders={
                "model": self._current_model,
                "code": self._working_code,
            },
        )

    # ====================================================================
    # TUYA BRANCH
    # ====================================================================
    def _make_cloud(self) -> TuyaIRCloud | None:
        creds = find_bms_creds(self.hass, self._bms_entry_id or None)
        if creds is None:
            return None
        self._bms_entry_id = creds.entry_id
        session = async_get_clientsession(self.hass)
        return TuyaIRCloud(
            session, creds.region, creds.client_id, creds.secret, creds.user_id
        )

    async def async_step_tuya(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if find_bms_creds(self.hass) is None:
            return self.async_abort(reason="no_bms_cloud")

        errors: dict[str, str] = {}
        if user_input is not None:
            self._infrared_id = user_input[CONF_INFRARED_ID].strip()
            cloud = self._make_cloud()
            if cloud is None:
                return self.async_abort(reason="no_bms_cloud")
            ok, _ = await cloud.async_test()
            if not ok:
                errors["base"] = "cannot_connect"
            else:
                remotes, msg = await cloud.list_remotes(self._infrared_id)
                if msg != "ok" or not remotes:
                    if msg != "ok":
                        _LOGGER.warning("Could not list remotes: %s", msg)
                    errors["base"] = "no_remotes"
                else:
                    self._remotes = remotes
                    return await self.async_step_tuya_select()

        return self.async_show_form(
            step_id="tuya",
            data_schema=vol.Schema({vol.Required(CONF_INFRARED_ID): str}),
            errors=errors,
        )

    async def async_step_tuya_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = {
            rid: _remote_label(r) for r in self._remotes if (rid := _remote_id(r))
        }
        if not options:
            return self.async_abort(reason="no_remotes")

        if user_input is not None:
            self._device_id = user_input[CONF_DEVICE_ID]
            self._name = user_input[CONF_NAME]
            await self.async_set_unique_id(self._device_id)
            self._abort_if_unique_id_configured()

            chosen = next(
                (r for r in self._remotes if _remote_id(r) == self._device_id), {}
            )
            self._category_id, self._category_name = _category_of(chosen)
            remote_name = chosen.get("remote_name") or ""

            cloud = self._make_cloud()
            is_ac = False
            if cloud is not None:
                status = await cloud.get_ac_status(self._infrared_id, self._device_id)
                is_ac = status is not None or _looks_like_ac(
                    self._category_id, remote_name, self._category_name
                )

            if is_ac:
                self._kind = KIND_CLIMATE
                self._category_name = "Air conditioner"
                if cloud is not None:
                    ok, msg = await cloud.send_ac_scene(
                        self._infrared_id, self._device_id,
                        power=1, mode=DEFAULT_MODE_INT, temp=DEFAULT_TEMP, wind=DEFAULT_WIND_INT,
                    )
                    if not ok:
                        _LOGGER.warning("AC test command failed: %s", msg)
            else:
                self._kind = KIND_REMOTE
                if cloud is not None:
                    cat_id, keys, msg = await cloud.list_keys(
                        self._infrared_id, self._device_id
                    )
                    if cat_id:
                        self._category_id = cat_id
                        self._category_name = CATEGORY_NAMES.get(cat_id, self._category_name)
                    power_key = next(
                        (k for k in keys if str(k.get("key_name", "")).lower() == "power"),
                        None,
                    )
                    if power_key:
                        await cloud.send_key(
                            self._infrared_id, self._device_id, self._category_id,
                            power_key.get("key_id"), power_key.get("key"),
                        )
            return await self.async_step_tuya_confirm()

        first_label = next(iter(options.values()), "IR Remote")
        default_name = first_label.split(" — ")[0]
        return self.async_show_form(
            step_id="tuya_select",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_ID): vol.In(options),
                    vol.Required(CONF_NAME, default=default_name): str,
                }
            ),
        )

    async def async_step_tuya_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=self._name,
                data={
                    CONF_BACKEND: BACKEND_TUYA,
                    CONF_BMS_ENTRY_ID: self._bms_entry_id,
                    CONF_INFRARED_ID: self._infrared_id,
                    CONF_DEVICE_ID: self._device_id,
                    CONF_NAME: self._name,
                    CONF_KIND: self._kind,
                    CONF_CATEGORY_ID: self._category_id,
                    CONF_CATEGORY_NAME: self._category_name,
                },
            )
        kind_label = (
            "konditsioner" if self._kind == KIND_CLIMATE else self._category_name
        )
        return self.async_show_form(
            step_id="tuya_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"kind": kind_label},
        )

    # ====================================================================
    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BmsSmartIROptionsFlow()


class BmsSmartIROptionsFlow(OptionsFlow):
    """Change the device code (and sensors) after setup — with a live test."""

    def __init__(self) -> None:
        self._catalog: dict[str, list[dict]] = {}
        self._status: str = ""
        self._selected: str | None = None

    @property
    def _dir(self) -> str:
        return os.path.dirname(__file__)

    async def _send_test(self, device_type: str, code: str) -> None:
        data = await async_load_code(self.hass, device_type, code)
        if not data:
            self._status = "⚠️ Не удалось загрузить файл кода."
            return
        commands = data.get("commands", {})
        if device_type == DEVICE_TYPE_MEDIA_PLAYER:
            command = (
                commands.get("volumeUp")
                or commands.get("mute")
                or commands.get("on")
                or commands.get("off")
            )
        else:
            command = representative_command(data)
        if not command:
            for value in commands.values():
                if isinstance(value, str):
                    command = value
                    break
        if not command:
            self._status = "⚠️ В этом коде нет команды для отправки."
            return
        cur = self.config_entry.data
        sent = await async_send_test(
            self.hass,
            cur[CONF_HOST],
            _decode(command, data.get("commandsEncoding", "Base64")) or b"",
            port=cur.get(CONF_PORT, DEFAULT_PORT),
            timeout=cur.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
        )
        if sent:
            self._status = (
                f"📡 Отправлен тестовый сигнал кода #{code}. Сработало? Тогда снимите "
                "галочку «Проверить» и нажмите «Отправить» — код сохранится. "
                "Не сработало — выберите другой код."
            )
        else:
            self._status = "⚠️ Не удалось отправить — проверьте связь с Broadlink."

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        data = self.config_entry.data
        if data.get(CONF_BACKEND) != BACKEND_BROADLINK:
            # Tuya entries have nothing to configure here.
            return self.async_create_entry(title="", data={})

        device_type = data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_CLIMATE)
        controller_name = data.get(CONF_CONTROLLER, CONTROLLER_BROADLINK)
        manufacturer = data.get(CONF_MANUFACTURER)

        if not self._catalog:
            self._catalog = await _catalog_map(self.hass, device_type, controller_name)
        candidates = self._catalog.get(manufacturer, [])
        current = {**data, **self.config_entry.options}
        current_code = current.get(CONF_DEVICE_CODE)

        if user_input is not None:
            new_code = user_input[CONF_DEVICE_CODE]
            self._selected = new_code
            if user_input.get("test_signal"):
                await self._send_test(device_type, new_code)
            else:
                opts: dict[str, Any] = {
                    CONF_DEVICE_CODE: new_code,
                    CONF_MODEL: next(
                        (e["model"] for e in candidates if e["code"] == new_code),
                        current.get(CONF_MODEL),
                    ),
                }
                if device_type == DEVICE_TYPE_CLIMATE:
                    for key in (
                        CONF_TEMPERATURE_SENSOR,
                        CONF_HUMIDITY_SENSOR,
                        CONF_POWER_SENSOR,
                    ):
                        if user_input.get(key):
                            opts[key] = user_input[key]
                return self.async_create_entry(title="", data=opts)

        if not candidates:
            return self.async_abort(reason="no_codes")

        options = [
            selector.SelectOptionDict(
                value=e["code"], label=f"{e['model']}  (#{e['code']})"
            )
            for e in candidates
        ]
        default_code = self._selected or current_code
        schema_dict: dict = {
            vol.Required(CONF_DEVICE_CODE, default=default_code): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options, mode=selector.SelectSelectorMode.DROPDOWN
                )
            ),
            vol.Optional("test_signal", default=False): selector.BooleanSelector(),
        }
        if device_type == DEVICE_TYPE_CLIMATE:
            schema_dict.update(_sensors_dict(current))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "manufacturer": manufacturer or "",
                "status": self._status,
            },
        )
