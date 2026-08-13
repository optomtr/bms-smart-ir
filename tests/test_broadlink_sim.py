"""The bench proves itself against the real client library.

Every test here drives the simulator through `broadlink` exactly as the
integration does. If a test passes with a stubbed transport it proves nothing
about the seam between our code and the device — so there are no stubs.
"""

from __future__ import annotations

import os
import sys

import broadlink
import pytest
from broadlink import exceptions as blx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from broadlink_sim import Farm  # noqa: E402

# A real "power on" packet from an RM4: 0x26 = IR, then the pulse table.
IR_PACKET = bytes.fromhex("260048004e1a1e1a1e341e1a1e1a1e1a1e341e341e1a1e000d05")

FAST = {"timeout": 1}


@pytest.fixture(autouse=True)
def _real_sockets(real_sockets):
    """These tests exist to exercise the wire, so the socket stays open."""


@pytest.fixture
def farm():
    with Farm(3, base_port=21000, models=["rm4mini", "rm4c", "rm4pro"]) as running:
        yield running


def connect(farm: Farm, index: int = 0):
    """Discover and authenticate exactly the way the integration will."""
    host, port = farm.address(index)
    device = broadlink.hello(host, port, **FAST)
    device.auth()
    return device


def test_hello_reports_the_real_model(farm: Farm):
    expected = [("RM4MINI", "RM4 mini"), ("RM4MINI", "RM4C mini"), ("RM4PRO", "RM4 pro")]
    for index, (type_name, model) in enumerate(expected):
        host, port = farm.address(index)
        device = broadlink.hello(host, port, **FAST)
        assert device.type == type_name
        assert device.model == model
        assert device.mac == farm.devices[index].mac
        assert device.name == farm.devices[index].name


def test_auth_starts_a_session(farm: Farm):
    device = connect(farm)
    assert device.id != 0
    assert farm.devices[0].stats.auths == 1


def test_send_data_arrives_byte_identical(farm: Farm):
    device = connect(farm, 2)
    device.send_data(IR_PACKET)

    log = farm.devices[2].ir_log
    assert len(log) == 1
    assert log[0].data == IR_PACKET


def test_check_sensors_round_trips(farm: Farm):
    simulated = farm.devices[1]
    simulated.temperature = 23.5
    simulated.humidity = 45.0

    readings = connect(farm, 1).check_sensors()

    assert readings["temperature"] == pytest.approx(23.5)
    assert readings["humidity"] == pytest.approx(45.0)
    assert simulated.stats.sensor_reads == 1


def test_check_sensors_handles_frost(farm: Farm):
    farm.devices[1].temperature = -3.5

    readings = connect(farm, 1).check_sensors()

    assert readings["temperature"] == pytest.approx(-3.5)


def test_device_without_a_sensor_reports_zero(farm: Farm):
    farm.devices[0].faults.zero_sensor = True

    readings = connect(farm, 0).check_sensors()

    assert readings == {"temperature": 0.0, "humidity": 0.0}


def test_offline_device_times_out(farm: Farm):
    device = connect(farm, 0)
    farm.devices[0].faults.offline = True

    with pytest.raises(blx.NetworkTimeoutError):
        device.send_data(IR_PACKET)


def test_expired_session_is_reported_then_recovers(farm: Farm):
    device = connect(farm, 0)
    farm.devices[0].faults.expire_session_after = 0

    with pytest.raises(blx.AuthorizationError):
        device.send_data(IR_PACKET)

    farm.devices[0].faults.expire_session_after = None
    device.auth()
    device.send_data(IR_PACKET)
    assert len(farm.devices[0].ir_log) == 1


def test_stalled_device_answers_nothing(farm: Farm):
    """Socket alive, service dead — the failure that needs a watchdog."""
    device = connect(farm, 0)
    farm.devices[0].faults.stall_after = 1

    device.send_data(IR_PACKET)
    with pytest.raises(blx.NetworkTimeoutError):
        device.send_data(IR_PACKET)


def test_corrupted_answer_is_rejected(farm: Farm):
    device = connect(farm, 0)
    farm.devices[0].faults.corrupt_rate = 1.0

    with pytest.raises(blx.DataValidationError):
        device.send_data(IR_PACKET)


def test_a_farm_of_sixty_answers_every_device():
    with Farm(60, base_port=21100) as farm:
        macs = set()
        for index in range(60):
            host, port = farm.address(index)
            device = broadlink.hello(host, port, **FAST)
            macs.add(device.mac)
        assert len(macs) == 60
