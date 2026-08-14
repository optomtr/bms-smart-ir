"""Test bench wiring.

The integration package and the simulator are imported from the repository, so
tests exercise the shipped files rather than a copy.
"""

from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO, os.path.join(REPO, "tools")):
    if path not in sys.path:
        sys.path.insert(0, path)

pytest_plugins = "pytest_homeassistant_custom_component"

# Import the integration once, here, rather than letting the first test pay for
# it: Home Assistant imports custom components in a worker thread, and that
# thread outlives the first test, which the harness then reports as a leak.
import custom_components.bms_smart_ir  # noqa: E402,F401
import custom_components.bms_smart_ir.config_flow  # noqa: E402,F401


@pytest.fixture(autouse=True)
def verify_cleanup(
    event_loop, expected_lingering_tasks: bool, expected_lingering_timers: bool
):
    """The harness' own cleanup check, minus one false alarm.

    `hass_ws_client` starts an aiohttp test server whose shutdown leaves Home
    Assistant's `_run_safe_shutdown_loop` daemon thread behind — reproducible
    with no code of ours involved at all. Everything else the harness checks
    (lingering tasks, timers, extra instances, and any other stray thread)
    still fails the test, which is what catches a hub task we forgot to stop.
    """
    from pytest_homeassistant_custom_component.plugins import (
        verify_cleanup as upstream,
    )

    checker = upstream.__wrapped__(
        event_loop, expected_lingering_tasks, expected_lingering_timers
    )
    next(checker)
    yield
    try:
        next(checker)
    except StopIteration:
        return
    except AssertionError as err:
        if "_run_safe_shutdown_loop" not in str(err):
            raise


@pytest.fixture
def real_sockets(socket_enabled):
    """The bench talks to the simulator over a real UDP socket on purpose.

    Home Assistant's harness blocks sockets so that a test cannot silently
    reach the network; here the socket IS the thing under test, so it is
    unblocked explicitly and only in the tests that drive the simulator.
    """
    return socket_enabled
