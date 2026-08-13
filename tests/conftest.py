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


@pytest.fixture
def real_sockets(socket_enabled):
    """The bench talks to the simulator over a real UDP socket on purpose.

    Home Assistant's harness blocks sockets so that a test cannot silently
    reach the network; here the socket IS the thing under test, so it is
    unblocked explicitly and only in the tests that drive the simulator.
    """
    return socket_enabled
