"""One physical Broadlink: one connection, one queue, one watchdog.

Everything that talks to a device goes through here. The rule this module
exists to enforce is that a Broadlink serves one request at a time: before it,
each entity opened its own session to the same box, so an air conditioner, a TV
and a sensor poll on one RM4 fought each other.

Design notes that are easy to undo by accident:

* IR is fire-and-forget. A timed-out transmission is NOT retried — repeating an
  IR frame physically repeats the action (a TV toggles back off). Only an
  explicit "control key expired" answer is retried, because that one proves the
  device did not act.
* A timeout is not proof of a dead socket. The session is dropped instantly
  only on a transport error; timeouts have to happen FAILURE_THRESHOLD times in
  a row first.
* Callers must be able to tell a lost command from a delivered one, so
  `async_send` returns a bool and never swallows the difference.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import broadlink
from broadlink import exceptions as blx

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    BACKOFF_SECONDS,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    DOMAIN,
    FAILURE_THRESHOLD,
    FIRST_HEARTBEAT_DELAY,
    GRACE_SECONDS,
    IR_GAP_MS,
    MAX_PARALLEL_IO,
    SENSOR_INTERVAL,
    SIGNAL_HUB_UPDATE,
    STARTUP_GRACE_SECONDS,
    STARTUP_STAGGER,
    UPTIME_SAMPLES,
    WATCHDOG_INTERVAL,
)

_LOGGER = logging.getLogger(__package__)

DATA_HUBS = "hubs"
DATA_IO_LIMIT = "io_limit"
DATA_WATCHDOG = "watchdog"

STATUS_CONNECTING = "connecting"
STATUS_ONLINE = "online"
STATUS_RECONNECTING = "reconnecting"
STATUS_UNAVAILABLE = "unavailable"


@dataclass
class _Pending:
    """A transmission waiting for its turn on the device."""

    data: bytes
    futures: list[asyncio.Future] = field(default_factory=list)


@dataclass
class HubStats:
    """What the panel shows and what a post-mortem needs."""

    sent: int = 0
    failed: int = 0
    coalesced: int = 0
    reconnects: int = 0
    last_ok: float | None = None
    last_error: str | None = None
    last_error_at: float | None = None


class BroadlinkHub:
    """Owns the connection to one Broadlink and everything sent over it."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        *,
        port: int = DEFAULT_PORT,
        timeout: int = DEFAULT_TIMEOUT,
        start_delay: float = 0.0,
    ) -> None:
        self.hass = hass
        self.host = host
        self.port = port
        self.timeout = timeout
        self.start_delay = start_delay
        # Instance copies so a test bench can run fast without monkeypatching
        # module globals; production always uses the constants.
        self.gap_ms = IR_GAP_MS
        self.sensor_interval = SENSOR_INTERVAL

        self.mac: bytes | None = None
        self.model: str | None = None
        self.devtype: int | None = None
        self.device_name: str | None = None
        self.has_sensor: bool | None = None
        self.sensors: dict[str, float] = {}
        self.stats = HubStats()
        # Bounded on purpose: an installation runs for years.
        self.uptime_log: deque[tuple[float, bool]] = deque(maxlen=UPTIME_SAMPLES)

        self._device: Any | None = None
        self._status = STATUS_CONNECTING
        self._since = time.monotonic()
        self._started_at = time.monotonic()
        self._failures = 0
        self._backoff_index = 0

        self._pending: dict[str, _Pending] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._sequence = 0
        self._last_tx = 0.0

        self._worker: asyncio.Task | None = None
        self._sensor_task: asyncio.Task | None = None
        self._connect_task: asyncio.Task | None = None
        self._io_lock = asyncio.Lock()
        self._listeners: set[Callable[[], None]] = set()
        self._refs = 0

    # ---- identity --------------------------------------------------------
    @property
    def hub_id(self) -> str:
        """Stable id for the device registry and the panel.

        The address, not the MAC: it is known before the first connection, so
        the device card and its history exist from the start even for a box
        that is offline right now. Replacing the hardware at the same address
        keeps the card; moving a box to a new address is handled explicitly by
        the panel, which renames the device instead of orphaning it.
        """
        return hub_key(self.host, self.port)

    @property
    def mac_text(self) -> str | None:
        if self.mac is None:
            return None
        return ":".join(f"{b:02X}" for b in self.mac)

    @property
    def status(self) -> str:
        return self._status

    @property
    def available(self) -> bool:
        """Available means 'do not paint the entities dead yet'.

        A Wi-Fi hiccup must not litter the history, so a device keeps its state
        through a grace window; a real outage still shows up after it.
        """
        if self._status == STATUS_ONLINE:
            return True
        grace = GRACE_SECONDS
        if time.monotonic() - self._started_at < STARTUP_GRACE_SECONDS:
            grace = STARTUP_GRACE_SECONDS
        return (time.monotonic() - self._since) < grace

    # ---- lifecycle -------------------------------------------------------
    def acquire(self) -> None:
        """Register one more user of this hub."""
        self._refs += 1

    def release(self) -> bool:
        """Drop one user; returns True when nobody is left."""
        self._refs = max(0, self._refs - 1)
        return self._refs == 0

    async def async_start(self, *, with_heartbeat: bool = True) -> None:
        if self._worker is None:
            self._worker = self.hass.async_create_background_task(
                self._run_worker(), f"{DOMAIN}_hub_{self.host}"
            )
        if not with_heartbeat:
            return
        if self._sensor_task is None:
            self._sensor_task = self.hass.async_create_background_task(
                self._run_sensor_loop(), f"{DOMAIN}_sensors_{self.host}"
            )

    async def async_stop(self) -> None:
        for task in (self._worker, self._sensor_task, self._connect_task):
            if task is not None:
                task.cancel()
        self._worker = self._sensor_task = self._connect_task = None
        for pending in self._pending.values():
            for future in pending.futures:
                if not future.done():
                    future.set_result(False)
        self._pending.clear()
        self._device = None

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(update)

        def _remove() -> None:
            self._listeners.discard(update)

        return _remove

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()
        async_dispatcher_send(self.hass, SIGNAL_HUB_UPDATE.format(self.hub_id))

    # ---- public API ------------------------------------------------------
    async def async_send(self, data: bytes, *, coalesce_key: str | None = None) -> bool:
        """Queue an IR frame. Returns True only if the device took it.

        `coalesce_key` collapses repeats that describe a STATE (an air
        conditioner re-sends its whole state on every change, so dragging the
        temperature is one transmission, not five). Button presses must not
        pass a key: two presses mean two presses.
        """
        if coalesce_key is None:
            self._sequence += 1
            coalesce_key = f"#{self._sequence}"

        future = self.hass.loop.create_future()
        pending = self._pending.get(coalesce_key)
        if pending is not None:
            # Same state, newer value: the older frame never goes out.
            self.stats.coalesced += 1
            pending.data = data
            pending.futures.append(future)
        else:
            self._pending[coalesce_key] = _Pending(data, [future])
            self._queue.put_nowait(coalesce_key)
        return await future

    async def async_read_sensors(self, *, force: bool = False) -> dict[str, float]:
        """Read temperature/humidity. Doubles as the heartbeat."""
        if self.has_sensor is False and not force:
            return {}
        return await self._run_io(self._read_sensors_sync, "sensors")

    def describe(self) -> dict[str, Any]:
        """Snapshot for the panel — no secrets, no raw network data."""
        return {
            "hub_id": self.hub_id,
            "host": self.host,
            "port": self.port,
            "mac": self.mac_text,
            "model": self.model,
            "name": self.device_name,
            "status": self._status,
            "available": self.available,
            "since": self._since,
            "has_sensor": self.has_sensor,
            "sensors": dict(self.sensors),
            "queue": self._queue.qsize(),
            "stats": {
                "sent": self.stats.sent,
                "failed": self.stats.failed,
                "coalesced": self.stats.coalesced,
                "reconnects": self.stats.reconnects,
                "last_ok": self.stats.last_ok,
                "last_error": self.stats.last_error,
                "last_error_at": self.stats.last_error_at,
            },
        }

    # ---- worker ----------------------------------------------------------
    async def _run_worker(self) -> None:
        if self.start_delay:
            # Sixty simultaneous handshakes at startup drown the network and
            # the event loop; spread them out.
            await asyncio.sleep(self.start_delay)
        while True:
            key = await self._queue.get()
            pending = self._pending.pop(key, None)
            if pending is None:
                continue
            await self._respect_gap()
            try:
                result = await self._transmit(pending.data)
            except asyncio.CancelledError:
                for future in pending.futures:
                    if not future.done():
                        future.set_result(False)
                raise
            except Exception as err:  # noqa: BLE001 - the worker must survive
                _LOGGER.exception("%s: unexpected send failure", self.host)
                self._record_error(err)
                result = False
            for future in pending.futures:
                if not future.done():
                    future.set_result(result)

    async def _respect_gap(self) -> None:
        """Keep gap_ms between transmissions — the emitter needs the pause."""
        elapsed = (time.monotonic() - self._last_tx) * 1000.0
        if elapsed < self.gap_ms:
            await asyncio.sleep((self.gap_ms - elapsed) / 1000.0)

    async def _transmit(self, data: bytes) -> bool:
        try:
            await self._run_io(lambda device: device.send_data(data), "send")
        except blx.AuthorizationError:
            # The device rebooted and forgot the session. It did not act on the
            # command, so re-sending is safe here (and only here).
            self._device = None
            try:
                await self._run_io(lambda device: device.send_data(data), "send")
            except Exception as err:  # noqa: BLE001
                self._record_error(err)
                return False
        except Exception as err:  # noqa: BLE001 - classified in _record_error
            self._record_error(err)
            return False

        self._last_tx = time.monotonic()
        self.stats.sent += 1
        self.stats.last_ok = time.time()
        self._mark_online()
        return True

    # ---- sensors / heartbeat --------------------------------------------
    async def _run_sensor_loop(self) -> None:
        """One request per interval that is both the sensor read and the ping.

        A separate liveness timer would double the traffic for nothing, and a
        box with no sensor still has to be watched — there it asks the device
        for its name instead, which emits no IR.
        """
        await asyncio.sleep(self.start_delay + FIRST_HEARTBEAT_DELAY)
        while True:
            try:
                await self.async_heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - the loop must not die
                self._record_error(err)
                self._notify()
            await asyncio.sleep(self.sensor_interval)

    async def async_heartbeat(self) -> None:
        """One liveness round; the loop above is only a timer around it."""
        if self.has_sensor is False:
            # No accessory to read, but the device still has to be watched.
            # `update()` asks for the name — no IR leaves the emitter.
            await self._run_io(lambda device: device.update(), "ping")
            self._notify()
            return

        readings = await self.async_read_sensors(force=self.has_sensor is None)
        if not readings:
            if self.has_sensor is None:
                self.has_sensor = False
            self._notify()
            return
        if self.has_sensor is None:
            # A box without the accessory answers 0/0 forever.
            self.has_sensor = any(value != 0 for value in readings.values())
        if self.has_sensor:
            self.sensors = readings
        self._notify()

    def _read_sensors_sync(self, device: Any) -> dict[str, float]:
        if not hasattr(device, "check_sensors"):
            return {}
        readings = device.check_sensors() or {}
        return {
            key: float(value)
            for key, value in readings.items()
            if isinstance(value, (int, float))
        }

    # ---- connection ------------------------------------------------------
    async def _run_io(self, action: Callable[[Any], Any], what: str) -> Any:
        """Run one blocking library call against a connected device."""
        async with self._io_lock:
            limit = _io_limit(self.hass)
            async with limit:
                device = self._device
                if device is None:
                    device = await self._async_connect()
                try:
                    return await self.hass.async_add_executor_job(action, device)
                except (blx.NetworkTimeoutError, blx.DataValidationError):
                    raise
                except (blx.AuthorizationError, blx.ConnectionClosedError):
                    self._device = None
                    raise
                except (ConnectionError, OSError):
                    # A transport error IS proof the socket is gone.
                    self._device = None
                    raise

    async def _async_connect(self) -> Any:
        loop_device = await self.hass.async_add_executor_job(self._connect_sync)
        self._device = loop_device
        self.mac = bytes(loop_device.mac)
        self.model = loop_device.model or loop_device.type
        self.devtype = loop_device.devtype
        self.device_name = loop_device.name or None
        self._mark_online()
        return loop_device

    def _connect_sync(self) -> Any:
        device = broadlink.hello(self.host, self.port, timeout=self.timeout)
        device.timeout = self.timeout
        device.auth()
        return device

    # ---- state -----------------------------------------------------------
    def _mark_online(self) -> None:
        self._failures = 0
        self._backoff_index = 0
        if self._status != STATUS_ONLINE:
            was = self._status
            self._status = STATUS_ONLINE
            self._since = time.monotonic()
            self.uptime_log.append((time.time(), True))
            if was != STATUS_CONNECTING:
                _LOGGER.info("%s: связь восстановлена (%s)", self.hub_id, self.model or "?")
            else:
                _LOGGER.info("%s: подключён (%s)", self.hub_id, self.model or "?")
            self._notify()

    def _record_error(self, err: Exception) -> None:
        self.stats.failed += 1
        self.stats.last_error = f"{type(err).__name__}: {err}"
        self.stats.last_error_at = time.time()
        self._failures += 1

        transport_dead = self._device is None
        _LOGGER.debug("%s: ошибка обмена (%s)", self.hub_id, self.stats.last_error)
        if transport_dead or self._failures >= FAILURE_THRESHOLD:
            # Only now do we admit the device is gone: a single timeout is
            # normal on Wi-Fi and must not take the session with it.
            self._device = None
            if self._status == STATUS_ONLINE:
                self._status = STATUS_RECONNECTING
                self._since = time.monotonic()
                self.uptime_log.append((time.time(), False))
                # One line per transition, not per failure: a device that is
                # down for a week must not fill the disk.
                _LOGGER.warning(
                    "%s: связь потеряна (%s), переподключаюсь",
                    self.hub_id,
                    self.stats.last_error,
                )
            elif self._status == STATUS_CONNECTING and not self.available:
                self._status = STATUS_UNAVAILABLE
                _LOGGER.warning(
                    "%s: не отвечает при запуске (%s)", self.hub_id, self.stats.last_error
                )
            self._notify()

    @property
    def backoff(self) -> float:
        index = min(self._backoff_index, len(BACKOFF_SECONDS) - 1)
        return BACKOFF_SECONDS[index]

    async def async_ensure_connection(self) -> None:
        """Called by the watchdog: never leave a device without a task."""
        if self._device is not None or self._connect_task is not None:
            return

        async def _reconnect() -> None:
            try:
                await asyncio.sleep(self.backoff)
                self._backoff_index += 1
                async with self._io_lock:
                    if self._device is None:
                        await self._async_connect()
                        self.stats.reconnects += 1
            except Exception as err:  # noqa: BLE001 - keep trying
                self._record_error(err)
            finally:
                # Only the task itself clears its own handle: clearing it from
                # elsewhere leaves a device with nobody working on it.
                self._connect_task = None

        self._connect_task = self.hass.async_create_background_task(
            _reconnect(), f"{DOMAIN}_reconnect_{self.host}"
        )


# ---- registry ------------------------------------------------------------
def _domain_data(hass: HomeAssistant) -> dict[str, Any]:
    return hass.data.setdefault(DOMAIN, {})


def _io_limit(hass: HomeAssistant) -> asyncio.Semaphore:
    """Cap concurrent blocking calls so the executor pool survives 60 hubs."""
    data = _domain_data(hass)
    limit = data.get(DATA_IO_LIMIT)
    if limit is None:
        limit = data[DATA_IO_LIMIT] = asyncio.Semaphore(MAX_PARALLEL_IO)
    return limit


def hub_key(host: str, port: int = DEFAULT_PORT) -> str:
    """Identity of one emitter.

    The port is part of it: every real Broadlink answers on 80, but a test
    bench runs many of them on one address, and a hub keyed by address alone
    silently merged all of them into one.
    """
    return host if port == DEFAULT_PORT else f"{host}:{port}"


def async_hubs(hass: HomeAssistant) -> dict[str, BroadlinkHub]:
    return _domain_data(hass).setdefault(DATA_HUBS, {})


async def async_get_hub(
    hass: HomeAssistant, host: str, *, port: int = DEFAULT_PORT, timeout: int = DEFAULT_TIMEOUT
) -> BroadlinkHub:
    """Return the hub for an address, creating and starting it once."""
    hubs = async_hubs(hass)
    key = hub_key(host, port)
    hub = hubs.get(key)
    if hub is None:
        hub = BroadlinkHub(
            hass,
            host,
            port=port,
            timeout=timeout,
            start_delay=len(hubs) * STARTUP_STAGGER,
        )
        hubs[key] = hub
        await hub.async_start()
        await async_start_watchdog(hass)
    hub.acquire()
    return hub


async def async_release_hub(
    hass: HomeAssistant, host: str, port: int = DEFAULT_PORT
) -> None:
    hubs = async_hubs(hass)
    key = hub_key(host, port)
    hub = hubs.get(key)
    if hub is None:
        return
    if hub.release():
        await hub.async_stop()
        hubs.pop(key, None)


async def async_start_watchdog(hass: HomeAssistant) -> None:
    """One task per Home Assistant that keeps every hub trying to reconnect."""
    data = _domain_data(hass)
    if data.get(DATA_WATCHDOG) is not None:
        return

    async def _watch() -> None:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            hubs = list(async_hubs(hass).values())
            waiting = [hub for hub in hubs if hub.status != STATUS_ONLINE]
            if waiting:
                _LOGGER.debug(
                    "Сторож: %d из %d устройств без связи, поднимаю подключение",
                    len(waiting),
                    len(hubs),
                )
            for hub in hubs:
                await hub.async_ensure_connection()

    data[DATA_WATCHDOG] = hass.async_create_background_task(
        _watch(), f"{DOMAIN}_watchdog"
    )


async def async_stop_watchdog(hass: HomeAssistant) -> None:
    data = _domain_data(hass)
    task = data.pop(DATA_WATCHDOG, None)
    if task is not None:
        task.cancel()
