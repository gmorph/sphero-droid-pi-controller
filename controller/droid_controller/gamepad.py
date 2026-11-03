"""Gamepad input: evdev reading in a thread, normalisation, and a scripted fake.

Both sources expose the same small surface: a state snapshot (connected flag,
normalised axes, time of the last event) and a queue of button press edges.
Axis names: lx, ly, rx, ry (sticks, -1.0..1.0, y negative is up), lt, rt
(triggers, 0.0..1.0), hx, hy (D-pad, raw -1, 0 or 1).
"""

import logging
import math
import queue
import select
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import evdev
from evdev import ecodes

from .config import GamepadConfig
from .mapping import normalise_axis, normalise_trigger

log = logging.getLogger(__name__)

AXIS_NAMES: Mapping[int, str] = {
    ecodes.ABS_X: "lx",
    ecodes.ABS_Y: "ly",
    ecodes.ABS_RX: "rx",
    ecodes.ABS_RY: "ry",
    ecodes.ABS_Z: "lt",
    ecodes.ABS_RZ: "rt",
    ecodes.ABS_HAT0X: "hx",
    ecodes.ABS_HAT0Y: "hy",
}
TRIGGER_AXES = frozenset({"lt", "rt"})
HAT_AXES = frozenset({"hx", "hy"})
HAT_PRESSES: Mapping[tuple[str, int], str] = {
    ("hx", -1): "dpad_left",
    ("hx", 1): "dpad_right",
    ("hy", -1): "dpad_up",
    ("hy", 1): "dpad_down",
}
BUTTON_NAMES: Mapping[int, str] = {
    ecodes.BTN_SOUTH: "a",
    ecodes.BTN_EAST: "b",
    ecodes.BTN_NORTH: "y",
    ecodes.BTN_WEST: "x",
    ecodes.BTN_TL: "lb",
    ecodes.BTN_TR: "rb",
    ecodes.BTN_SELECT: "back",
    ecodes.BTN_START: "start",
    ecodes.BTN_MODE: "guide",
}
CENTRED_AXES: Mapping[str, float] = {name: 0.0 for name in AXIS_NAMES.values()}

SELECT_TIMEOUT = 0.5


@dataclass(frozen=True)
class GamepadState:
    connected: bool
    axes: Mapping[str, float]
    last_event: float
    """time.monotonic() of the last event received (or of the connection)."""


class GamepadSource(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def snapshot(self) -> GamepadState: ...

    def pop_presses(self) -> list[str]:
        """Return and clear the button press edges received since the last call."""
        ...


def is_gamepad(device: evdev.InputDevice) -> bool:
    capabilities = device.capabilities(absinfo=False)
    axes = capabilities.get(ecodes.EV_ABS, [])
    keys = capabilities.get(ecodes.EV_KEY, [])
    return ecodes.ABS_X in axes and ecodes.ABS_Y in axes and ecodes.BTN_SOUTH in keys


def find_gamepad(preferred_names: Sequence[str]) -> evdev.InputDevice | None:
    """Open the first gamepad device, preferring names containing one of preferred_names."""
    candidates: list[evdev.InputDevice] = []
    for path in sorted(evdev.list_devices()):
        try:
            device = evdev.InputDevice(path)
        except OSError as exc:
            log.debug("Cannot open %s: %s", path, exc)
            continue
        if is_gamepad(device):
            candidates.append(device)
        else:
            device.close()
    if not candidates:
        return None
    wanted = [name.lower() for name in preferred_names]
    chosen = next(
        (d for d in candidates if any(w in d.name.lower() for w in wanted)),
        candidates[0],
    )
    for device in candidates:
        if device is not chosen:
            device.close()
    return chosen


class EvdevGamepad:
    """Reads a gamepad in a daemon thread; reconnects when the device vanishes."""

    def __init__(
        self,
        config: GamepadConfig,
        wake: threading.Event,
        on_connection: Callable[[bool], None] | None = None,
    ) -> None:
        self._config = config
        self._wake = wake
        self._on_connection = on_connection or (lambda _connected: None)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._presses: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._connected = False
        self._axes: dict[str, float] = dict(CENTRED_AXES)
        self._ranges: dict[str, tuple[int, int]] = {}
        self._last_event = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="gamepad", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2 * SELECT_TIMEOUT)

    def snapshot(self) -> GamepadState:
        with self._lock:
            return GamepadState(self._connected, dict(self._axes), self._last_event)

    def pop_presses(self) -> list[str]:
        presses = []
        while True:
            try:
                presses.append(self._presses.get_nowait())
            except queue.Empty:
                return presses

    def _run(self) -> None:
        while not self._stop.is_set():
            device = find_gamepad(self._config.preferred_names)
            if device is None:
                self._stop.wait(self._config.rescan_interval)
                continue
            log.info("Gamepad connected: %s (%s)", device.name, device.path)
            self._on_connected(device)
            try:
                self._read_until_stopped(device)
            except OSError as exc:
                log.warning("Gamepad lost: %s", exc)
            finally:
                device.close()
                self._on_disconnected()

    def _on_connected(self, device: evdev.InputDevice) -> None:
        ranges = {}
        for code, name in AXIS_NAMES.items():
            try:
                info = device.absinfo(code)
            except OSError:
                continue
            ranges[name] = (info.min, info.max)
        with self._lock:
            self._ranges = ranges
            self._axes = dict(CENTRED_AXES)
            self._connected = True
            self._last_event = time.monotonic()
        self._wake.set()
        self._on_connection(True)

    def _on_disconnected(self) -> None:
        with self._lock:
            self._connected = False
            self._axes = dict(CENTRED_AXES)
        self._wake.set()
        self._on_connection(False)

    def _read_until_stopped(self, device: evdev.InputDevice) -> None:
        while not self._stop.is_set():
            readable, _, _ = select.select([device.fd], [], [], SELECT_TIMEOUT)
            if not readable:
                continue
            for event in device.read():
                self._handle(event)

    def _handle(self, event: evdev.InputEvent) -> None:
        if event.type == ecodes.EV_ABS and event.code in AXIS_NAMES:
            self._handle_axis(AXIS_NAMES[event.code], event.value)
        elif event.type == ecodes.EV_KEY and event.code in BUTTON_NAMES and event.value == 1:
            self._press(BUTTON_NAMES[event.code])
        with self._lock:
            self._last_event = time.monotonic()

    def _handle_axis(self, name: str, raw: int) -> None:
        if name in HAT_AXES:
            with self._lock:
                self._axes[name] = float(raw)
            if raw != 0:
                self._press(HAT_PRESSES[(name, raw)])
            return
        minimum, maximum = self._ranges.get(name, (0, 0))
        if name in TRIGGER_AXES:
            value = normalise_trigger(raw, minimum, maximum)
        else:
            value = normalise_axis(raw, minimum, maximum)
        with self._lock:
            was_centred = self._left_stick_centred()
            self._axes[name] = value
            now_centred = self._left_stick_centred()
        if now_centred and not was_centred:
            self._wake.set()

    def _left_stick_centred(self) -> bool:
        return math.hypot(self._axes["lx"], self._axes["ly"]) <= self._config.dead_zone

    def _press(self, name: str) -> None:
        self._presses.put(name)
        self._wake.set()


@dataclass(frozen=True)
class ScriptStep:
    """At `at` seconds after start, apply `axes` (which persist) and optionally press a button."""

    at: float
    axes: Mapping[str, float] = field(default_factory=dict)
    press: str | None = None


class ScriptedGamepad:
    """Replays a fixed sequence of stick positions and button presses, then requests shutdown."""

    FINISH_MARGIN = 0.5

    def __init__(self, steps: Sequence[ScriptStep], stop: threading.Event, wake: threading.Event) -> None:
        self._steps = sorted(steps, key=lambda s: s.at)
        self._started = time.monotonic()
        self._popped = 0
        self._timer = threading.Timer(self._steps[-1].at + self.FINISH_MARGIN, self._finish)
        self._timer.daemon = True
        self._stop = stop
        self._wake = wake

    def start(self) -> None:
        self._started = time.monotonic()
        self._popped = 0
        self._timer.start()

    def stop(self) -> None:
        self._timer.cancel()

    def _finish(self) -> None:
        log.info("Scripted gamepad sequence finished")
        self._stop.set()
        self._wake.set()

    def _elapsed(self) -> float:
        return time.monotonic() - self._started

    def snapshot(self) -> GamepadState:
        elapsed = self._elapsed()
        axes = dict(CENTRED_AXES)
        last_event = self._started
        for step in self._steps:
            if step.at > elapsed:
                break
            axes.update(step.axes)
            last_event = self._started + step.at
        return GamepadState(True, axes, last_event)

    def pop_presses(self) -> list[str]:
        elapsed = self._elapsed()
        presses = []
        while self._popped < len(self._steps) and self._steps[self._popped].at <= elapsed:
            step = self._steps[self._popped]
            if step.press:
                presses.append(step.press)
            self._popped += 1
        return presses


DEMO_SCRIPT: Sequence[ScriptStep] = (
    ScriptStep(0.5, {"ly": -0.5}),
    ScriptStep(1.0, {"lx": 1.0, "ly": 0.0}),
    ScriptStep(1.5, {"lx": -0.7, "ly": 0.7}),
    ScriptStep(2.0, {"lx": 0.0, "ly": 0.0}),
    ScriptStep(2.3, press="start"),
    ScriptStep(2.6, press="start"),
    ScriptStep(3.0, {"lt": 1.0, "lx": 1.0}),
    ScriptStep(4.0, {"lt": 0.0, "lx": 0.0}),
    ScriptStep(4.3, {"rx": 0.5}),
    ScriptStep(4.6, {"rx": -1.0}),
    ScriptStep(4.9, {"rx": 0.0}),
    ScriptStep(5.2, press="a"),
    ScriptStep(5.4, press="b"),
    ScriptStep(5.6, press="x"),
    ScriptStep(5.8, press="y"),
    ScriptStep(6.0, {"hy": -1.0}, press="dpad_up"),
    ScriptStep(6.2, {"hy": 1.0}, press="dpad_down"),
    ScriptStep(6.4, {"hy": 0.0, "hx": -1.0}, press="dpad_left"),
    ScriptStep(6.6, {"hx": 1.0}, press="dpad_right"),
    ScriptStep(6.8, {"hx": 0.0}, press="lb"),
    ScriptStep(7.1, press="rb"),
    ScriptStep(7.4, {"ly": -1.0}),
    ScriptStep(7.8, press="guide"),
    ScriptStep(8.1, {"ly": 0.0}),
    ScriptStep(8.4, press="back"),  # opens the droid menu
    ScriptStep(9.0, {"ly": 1.0}),  # stick down: next item
    ScriptStep(9.3, {"ly": 0.0}),
    ScriptStep(9.6, press="dpad_down"),
    ScriptStep(9.9, press="dpad_up"),
    ScriptStep(10.2, press="a"),  # pick the second droid
)
