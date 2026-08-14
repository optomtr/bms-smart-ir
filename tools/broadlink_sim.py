"""Virtual Broadlink RM4 devices that speak the real protocol.

The integration talks to hardware through the `broadlink` library, so a test
bench is only worth something if that same library can drive it unmodified:
same handshake, same AES session, same framing, same error codes. Everything
here is written against `broadlink/device.py` and `broadlink/remote.py`.

Fault injection is the point of the bench, not a bonus: a device that always
answers proves nothing about an installation with 60 of them behind Wi-Fi.
Every knob in `Faults` reproduces a failure we have seen in the field.

Run a farm for manual work against Home Assistant:

    python tools/broadlink_sim.py --count 60 --json devices.json

Devices bind to 127.0.0.1 on ports 20000+ rather than the real port 80, which
would need root. `broadlink.hello(ip, port=...)` takes the port, so the client
side is unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import random
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger("broadlink_sim")

# Shared secret every Broadlink device ships with; the session key is
# negotiated on top of it (broadlink/device.py).
INIT_KEY = bytes.fromhex("097628343fe99e23765c1513accf8b02")
INIT_VECT = bytes.fromhex("562e17996d093d28ddb3ba695a2e6f58")

MAGIC = bytes.fromhex("5aa5aa555aa5aa55")

CMD_AUTH = 0x65
CMD_SESSION = 0x6A

# Payload commands inside a 0x6a packet (rmminib framing).
OP_UPDATE = 0x01
OP_SEND_DATA = 0x02
OP_ENTER_LEARNING = 0x03
OP_CHECK_DATA = 0x04
OP_CHECK_SENSORS = 0x24

ERR_CONTROL_KEY_EXPIRED = -7
ERR_STORAGE = -5

# Product ids the customer actually installs. Values from broadlink/__init__.py
# SUPPORTED_TYPES — a wrong id makes the library build the wrong class.
MODELS: dict[str, tuple[int, str, bool]] = {
    # key: (devtype, display model, has temperature/humidity sensor)
    "rm4mini": (0x51DA, "RM4 mini", False),
    "rm4c": (0x520D, "RM4C mini", True),
    "rm4pro": (0x520B, "RM4 pro", True),
}


@dataclass
class Faults:
    """Ways a device misbehaves. All are live-switchable from a test."""

    offline: bool = False
    """Answers nothing at all — unplugged, or Wi-Fi gone."""

    drop_rate: float = 0.0
    """Fraction of requests silently dropped (lossy Wi-Fi)."""

    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    """Delay before answering; jitter is added uniformly on top."""

    auth_extra_ms: float = 0.0
    """Handshakes are slower than commands on a loaded device."""

    stall_after: int | None = None
    """Stop answering after N session commands — the classic 'alive socket,
    dead service'. Cleared by re-auth."""

    corrupt_rate: float = 0.0
    """Fraction of answers sent with a broken header checksum."""

    expire_session_after: int | None = None
    """Answer 'control key is expired' after N commands until the client
    authenticates again."""

    sensor_silent: bool = False
    """Answer IR but never sensor reads (seen on RM4 mini clones)."""

    zero_sensor: bool = False
    """Report 0.0/0.0 — what a device without the sensor returns."""


@dataclass
class IrEvent:
    """One IR transmission as the device saw it."""

    monotonic: float
    data: bytes


@dataclass
class Stats:
    requests: int = 0
    dropped: int = 0
    auths: int = 0
    sensor_reads: int = 0
    rejected_expired: int = 0


class VirtualRM4(asyncio.DatagramProtocol):
    """One virtual RM4 on its own UDP port."""

    def __init__(
        self,
        *,
        mac: bytes,
        model: str = "rm4pro",
        name: str = "BMS Sim",
        temperature: float = 23.5,
        humidity: float = 45.0,
        faults: Faults | None = None,
        rng: random.Random | None = None,
        drift: bool = False,
    ) -> None:
        devtype, model_name, has_sensor = MODELS[model]
        self.mac = mac
        self.model = model
        self.model_name = model_name
        self.devtype = devtype
        self.has_sensor = has_sensor
        self.name = name
        self.temperature = temperature
        self.humidity = humidity
        self.faults = faults or Faults()
        self._rng = rng or random.Random(0)
        self.drift = drift
        self._drift_phase = (rng or random.Random(0)).random() * 6.28

        self.ir_log: list[IrEvent] = []
        self.stats = Stats()

        self.host = ""
        self.port = 0
        self._transport: asyncio.DatagramTransport | None = None
        self._session_key = INIT_KEY
        self._session_id = 0
        self._session_commands = 0
        self._learning: bytes | None = None
        self._pending_learned: bytes | None = None

    # ---- plumbing --------------------------------------------------------
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]
        self.host, self.port = transport.get_extra_info("sockname")[:2]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.stats.requests += 1
        if self.faults.offline:
            self.stats.dropped += 1
            return
        if self.faults.drop_rate and self._rng.random() < self.faults.drop_rate:
            self.stats.dropped += 1
            return

        try:
            reply = self._handle(data)
        except Exception:  # noqa: BLE001 - a bench must never take the loop down
            _LOGGER.exception("simulator failed on a request")
            return
        if reply is None:
            self.stats.dropped += 1
            return

        delay = self._delay_for(data)
        if delay > 0:
            asyncio.get_running_loop().call_later(
                delay, self._send_now, reply, addr
            )
        else:
            self._send_now(reply, addr)

    def _delay_for(self, data: bytes) -> float:
        faults = self.faults
        delay = faults.latency_ms
        if len(data) >= 0x38 and _u16(data, 0x26) == CMD_AUTH:
            delay += faults.auth_extra_ms
        if faults.jitter_ms:
            delay += self._rng.uniform(0, faults.jitter_ms)
        return delay / 1000.0

    def _send_now(self, reply: bytes, addr: tuple[str, int]) -> None:
        if self._transport is None or self.faults.offline:
            return
        if self.faults.corrupt_rate and self._rng.random() < self.faults.corrupt_rate:
            broken = bytearray(reply)
            broken[0x20] ^= 0xFF
            reply = bytes(broken)
        self._transport.sendto(reply, addr)

    # ---- protocol --------------------------------------------------------
    def _handle(self, data: bytes) -> bytes | None:
        if len(data) == 0x30 and data[0x26] == 6:
            return self._discovery_reply()
        if len(data) < 0x38 or data[0x00:0x08] != MAGIC:
            return None

        command = _u16(data, 0x26)
        if command == CMD_AUTH:
            return self._auth_reply(data)
        if command == CMD_SESSION:
            return self._session_reply(data)
        return None

    def _discovery_reply(self) -> bytes:
        """Answer a scan. Layout read back by broadlink.device.scan()."""
        packet = bytearray(0x80)
        packet[0x34:0x36] = self.devtype.to_bytes(2, "little")
        packet[0x3A:0x40] = self.mac[::-1]
        name = self.name.encode("utf-8")[:0x3E]
        packet[0x40 : 0x40 + len(name)] = name
        packet[0x7F] = 0  # not locked
        _stamp_checksum(packet)
        return bytes(packet)

    def _auth_reply(self, request: bytes) -> bytes:
        self.stats.auths += 1
        self._session_key = bytes(self._rng.getrandbits(8) for _ in range(16))
        self._session_id = self._rng.getrandbits(31) or 1
        self._session_commands = 0

        payload = bytearray(0x14)
        payload[0x00:0x04] = self._session_id.to_bytes(4, "little")
        payload[0x04:0x14] = self._session_key
        return self._pack(request, payload, key=INIT_KEY)

    def _session_reply(self, request: bytes) -> bytes | None:
        faults = self.faults
        if int.from_bytes(request[0x30:0x34], "little") != self._session_id:
            return self._pack(request, b"", err=ERR_CONTROL_KEY_EXPIRED)

        self._session_commands += 1
        if faults.stall_after is not None and self._session_commands > faults.stall_after:
            return None
        if (
            faults.expire_session_after is not None
            and self._session_commands > faults.expire_session_after
        ):
            self.stats.rejected_expired += 1
            return self._pack(request, b"", err=ERR_CONTROL_KEY_EXPIRED)

        payload = _decrypt(self._session_key, request[0x38:])
        # rmminib framing: <total length><command><data>
        if len(payload) < 6:
            return None
        p_len = struct.unpack("<H", payload[:2])[0]
        op = struct.unpack("<I", payload[2:6])[0]
        body = payload[6 : max(p_len + 2, 6)]

        data = self._run_op(op, body)
        if data is None:
            return self._pack(request, b"", err=ERR_STORAGE)
        return self._pack(request, _frame(data))

    def _run_op(self, op: int, body: bytes) -> bytes | None:
        if op == OP_SEND_DATA:
            self.ir_log.append(IrEvent(time.monotonic(), bytes(body)))
            return b""
        if op == OP_CHECK_SENSORS:
            return self._sensor_data()
        if op == OP_UPDATE:
            buffer = bytearray(0x88)
            name = self.name.encode("utf-8")[:0x3E]
            buffer[0x48 : 0x48 + len(name)] = name
            return bytes(buffer)
        if op == OP_ENTER_LEARNING:
            self._learning = None
            return b""
        if op == OP_CHECK_DATA:
            # Real devices answer with an error until a code was captured.
            if self._pending_learned is None:
                return None
            captured, self._pending_learned = self._pending_learned, None
            return captured
        return None

    def _sensor_data(self) -> bytes | None:
        if self.faults.sensor_silent:
            return None
        self.stats.sensor_reads += 1
        # An RM4 mini has no accessory and answers zeroes — the bench has to
        # do the same, otherwise a third of the fleet lies about having a
        # thermometer and the "only where the hardware has one" path is never
        # exercised.
        blind = self.faults.zero_sensor or not self.has_sensor
        temperature = 0.0 if blind else self._reading(self.temperature)
        humidity = 0.0 if blind else self._reading(self.humidity, span=2.0)
        # broadlink.remote.rm4mini.check_sensors reads whole + hundredths and
        # adds them, so the split has to floor: -3.5 is (-4, 50), not (-3, 50).
        return struct.pack("<bbBB", *_split_decimal(temperature), *_split_decimal(humidity))

    def _pack(
        self, request: bytes, payload: bytes, *, err: int = 0, key: bytes | None = None
    ) -> bytes:
        packet = bytearray(0x38)
        packet[0x00:0x08] = MAGIC
        packet[0x22:0x24] = struct.pack("<h", err)
        packet[0x24:0x26] = self.devtype.to_bytes(2, "little")
        packet[0x26:0x28] = request[0x26:0x28]
        packet[0x28:0x2A] = request[0x28:0x2A]
        packet[0x2A:0x30] = self.mac[::-1]
        packet[0x30:0x34] = self._session_id.to_bytes(4, "little")
        if payload:
            packet[0x34:0x36] = (sum(payload, 0xBEAF) & 0xFFFF).to_bytes(2, "little")
            padding = (16 - len(payload)) % 16
            packet += _encrypt(key or self._session_key, bytes(payload) + bytes(padding))
        _stamp_checksum(packet)
        return bytes(packet)

    # ---- bench helpers ---------------------------------------------------
    def feed_learned_code(self, data: bytes) -> None:
        """Pretend a remote was pointed at the device (for future learning)."""
        self._pending_learned = data

    def _reading(self, base: float, span: float = 0.6) -> float:
        """A slowly wandering value, so charts on the bench are not flat.

        Off by default: tests assert exact readings.
        """
        if not self.drift:
            return base
        phase = (time.monotonic() / 180.0) + self._drift_phase
        return round(base + math.sin(phase) * span, 2)

    def reboot(self) -> None:
        """Forget the session the way a power-cycled device does.

        The client keeps using the old control id and gets 'key expired' until
        it authenticates again — the one failure where re-sending is safe.
        """
        self._session_key = INIT_KEY
        self._session_id = 0
        self._session_commands = 0

    def ir_gaps_ms(self) -> list[float]:
        """Milliseconds between consecutive IR transmissions."""
        stamps = [event.monotonic for event in self.ir_log]
        return [(b - a) * 1000.0 for a, b in zip(stamps, stamps[1:])]

    def reset(self) -> None:
        self.ir_log.clear()
        self.stats = Stats()


def _split_decimal(value: float) -> tuple[int, int]:
    """Split a reading into (whole, hundredths) so whole + hundredths/100 == value."""
    whole = math.floor(value)
    hundredths = int(round((value - whole) * 100))
    if hundredths >= 100:  # 22.999 must not become (22, 100)
        whole += 1
        hundredths = 0
    return whole, hundredths


def _frame(data: bytes) -> bytes:
    """Wrap payload data the way an RM4 answers (rmminib framing)."""
    return struct.pack("<H", len(data) + 4) + bytes(4) + data


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little")


def _stamp_checksum(packet: bytearray) -> None:
    packet[0x20:0x22] = b"\x00\x00"
    checksum = sum(packet, 0xBEAF) & 0xFFFF
    packet[0x20:0x22] = checksum.to_bytes(2, "little")


def _encrypt(key: bytes, payload: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(INIT_VECT), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(payload) + encryptor.finalize()


def _decrypt(key: bytes, payload: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(INIT_VECT), backend=default_backend())
    decryptor = cipher.decryptor()
    return decryptor.update(payload) + decryptor.finalize()


class Farm:
    """A set of virtual devices on their own event loop thread.

    A thread, not the caller's loop: the `broadlink` client is blocking, and
    tests drive it straight from the test body.
    """

    def __init__(
        self,
        count: int = 1,
        *,
        host: str = "127.0.0.1",
        base_port: int = 20000,
        models: list[str] | None = None,
        seed: int = 1,
        name_prefix: str = "BMS Sim",
        drift: bool = False,
    ) -> None:
        self.host = host
        self.base_port = base_port
        rng = random.Random(seed)
        model_cycle = models or list(MODELS)
        self.devices: list[VirtualRM4] = [
            VirtualRM4(
                mac=bytes([0x24, 0xDF, 0xA7, (index >> 8) & 0xFF, index & 0xFF, 0x01]),
                model=model_cycle[index % len(model_cycle)],
                name=f"{name_prefix} {index + 1}",
                temperature=round(rng.uniform(18.0, 28.0), 2),
                humidity=round(rng.uniform(30.0, 60.0), 2),
                rng=random.Random(seed + index),
                drift=drift,
            )
            for index in range(count)
        ]
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._transports: list[asyncio.DatagramTransport] = []
        self._start_error: Exception | None = None

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> Farm:
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
        self._thread.start()
        if not ready.wait(timeout=10):
            raise RuntimeError("simulator farm did not start")
        if self._start_error is not None:
            raise self._start_error
        return self

    def _run(self, ready: threading.Event) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._bind_all())
        except Exception as err:  # noqa: BLE001 - reported to the caller thread
            self._start_error = err
            ready.set()
            return
        ready.set()
        self._loop.run_forever()

    async def _bind_all(self) -> None:
        loop = asyncio.get_running_loop()
        for index, device in enumerate(self.devices):
            transport, _ = await loop.create_datagram_endpoint(
                lambda device=device: device,
                local_addr=(self.host, self.base_port + index),
            )
            self._transports.append(transport)  # type: ignore[arg-type]

    def stop(self) -> None:
        """Release the ports before returning.

        `transport.close()` only schedules the close, so stopping the loop in
        the same callback leaves the sockets bound — the next farm on the same
        ports then fails with 'address already in use'.
        """
        if self._loop is None:
            return
        loop = self._loop
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(self._close_all(), loop).result(timeout=5)
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        loop.close()
        self._transports.clear()
        self._loop = None

    async def _close_all(self) -> None:
        for transport in self._transports:
            transport.close()
        await asyncio.sleep(0)  # let the loop run the close callbacks

    def __enter__(self) -> Farm:
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.stop()

    # ---- inspection ------------------------------------------------------
    def address(self, index: int = 0) -> tuple[str, int]:
        return self.host, self.base_port + index

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "index": index,
                "host": self.host,
                "port": self.base_port + index,
                "mac": ":".join(f"{b:02X}" for b in device.mac),
                "model": device.model_name,
                "devtype": hex(device.devtype),
                "sensor": device.has_sensor,
                "temperature": device.temperature,
                "humidity": device.humidity,
            }
            for index, device in enumerate(self.devices)
        ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run virtual Broadlink RM4 devices")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--base-port", type=int, default=20000)
    parser.add_argument(
        "--models",
        default=",".join(MODELS),
        help=f"comma separated, any of: {', '.join(MODELS)}",
    )
    parser.add_argument("--json", help="write the device list to this file")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--drift",
        action="store_true",
        help="медленно менять показания датчиков — чтобы графики были живыми",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    farm = Farm(
        args.count,
        host=args.host,
        base_port=args.base_port,
        models=[m.strip() for m in args.models.split(",") if m.strip()],
        seed=args.seed,
        drift=args.drift,
    )
    farm.start()
    listing = farm.describe()
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(listing, handle, ensure_ascii=False, indent=2)
    for item in listing:
        _LOGGER.info(
            "%s:%s  %-10s %s  %s",
            item["host"],
            item["port"],
            item["model"],
            item["mac"],
            "sensor" if item["sensor"] else "no sensor",
        )
    _LOGGER.info("%d device(s) running — Ctrl+C to stop", len(listing))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        farm.stop()


if __name__ == "__main__":
    main()
