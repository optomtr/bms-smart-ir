"""The sidebar panel: registration only, the interface lives in panel/panel.js.

Registered once per Home Assistant rather than per config entry — several
appliances share one panel, and Home Assistant sets entries up concurrently, so
the slot is claimed before the first await.
"""

from __future__ import annotations

import logging
import os

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.setup import async_setup_component

from .const import DOMAIN

_LOGGER = logging.getLogger(__package__)

PANEL_URL_PATH = "bms-ir"
PANEL_TITLE = "BMS ИК-пульты"
PANEL_ICON = "mdi:remote"
PANEL_COMPONENT = "bms-ir-panel"
STATIC_URL_PATH = "/bms_smart_ir_panel"

DATA_PANEL_REGISTERED = "panel_registered"


async def async_setup_panel(hass: HomeAssistant) -> None:
    """Serve the panel and put it in the sidebar.

    The panel is a convenience; controlling the appliances is the product. A
    frontend that cannot be set up (a headless test rig, a broken install) must
    therefore never take the integration down with it.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_PANEL_REGISTERED):
        return
    domain_data[DATA_PANEL_REGISTERED] = True

    panel_dir = os.path.join(os.path.dirname(__file__), "panel")
    integration = await async_get_integration(hass, DOMAIN)
    version = integration.version or "dev"

    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL_PATH, panel_dir, cache_headers=False)]
    )

    # Set the dependency up here rather than waiting for it: a waiter that
    # never fires (a headless install without the frontend) would sit in the
    # task list for the life of the process.
    if not await async_setup_component(hass, "panel_custom", {}):
        _LOGGER.warning(
            "Фронтенд недоступен — панель не зарегистрирована; "
            "управление приборами работает как обычно"
        )
        return

    from homeassistant.components import panel_custom

    try:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL_URL_PATH,
            webcomponent_name=PANEL_COMPONENT,
            sidebar_title=PANEL_TITLE,
            sidebar_icon=PANEL_ICON,
            # The version is part of the URL so a browser cannot serve a
            # cached panel from a previous release against a new backend.
            module_url=f"{STATIC_URL_PATH}/panel.js?v={version}",
            require_admin=False,
            config={"version": version},
        )
        _LOGGER.info("Панель зарегистрирована: /%s (v%s)", PANEL_URL_PATH, version)
    except ValueError:
        # Already registered (a reload, or a second start).
        _LOGGER.debug("Panel %s was already registered", PANEL_URL_PATH)
