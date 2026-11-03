"""What the controller is doing right now, for the status screen and the gamepad LED.

The control loop publishes changes through `Status.update`; the screen and LED
subscribe with `Status.add_listener`. Listeners run on the updating thread, so they
must be quick: the screen listener only wakes its own rendering thread.
"""

import dataclasses
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import NamedTuple


class Phase(Enum):
    STARTING = auto()
    SCANNING = auto()
    CONNECTED = auto()
    SLEEPING = auto()
    STOPPED = auto()


class Charger(NamedTuple):
    """How a droid takes its charge, in the words the log and the screen use for it."""

    name: str
    """For the log: "USB cable" or "charging cradle"."""
    tag: str
    """Short token for the facts row on the screen."""
    warning: str
    """Standing warning on the bottom row while charging; must fit a 128 px wide screen."""


USB_CABLE = Charger("USB cable", "USB", "USB IN: unplug to drive")
"""R2-D2 and R2-Q5 charge from a cable plugged into the droid."""
CRADLE = Charger("charging cradle", "Cradle", "ON CRADLE: lift to drive")
"""BB-8 and BB-9E charge by induction on a cradle; the cable powers the cradle."""


@dataclass(frozen=True)
class StatusSnapshot:
    phase: Phase = Phase.STARTING
    phase_since: float = 0.0
    """time.monotonic() when the phase last changed."""
    attempt: int = 0
    """Connection attempt number while scanning."""
    droid: str | None = None
    battery: str = ""
    """Short battery label, e.g. "4.1V" or "LOW"."""
    charging: bool = False
    """The droid is taking charge, so it will not drive or change stance."""
    charger: Charger = USB_CABLE
    """How the connected droid charges; picks the wording of the charging warning."""
    gamepad: bool = False
    heading: int = 0
    speed: int = 0
    aiming: bool = False
    stance: str | None = None
    led_on: bool = True
    message: str = ""
    """The most recent event worth showing (an animation name, "Aim reset", a retry delay)."""
    message_at: float = 0.0
    menu_items: tuple[str, ...] = ()
    """When not empty, the droid chooser is open and replaces the drive readout on the screen."""
    menu_index: int = 0
    menu_scanning: bool = False


Listener = Callable[[StatusSnapshot], None]


class Status:
    """Thread-safe holder of the latest StatusSnapshot that notifies listeners on change."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = StatusSnapshot(phase_since=time.monotonic())
        self._listeners: list[Listener] = []

    def add_listener(self, listener: Listener) -> None:
        with self._lock:
            self._listeners.append(listener)
            snapshot = self._snapshot
        listener(snapshot)

    def snapshot(self) -> StatusSnapshot:
        with self._lock:
            return self._snapshot

    def update(self, **changes: object) -> None:
        """Replace the given fields; listeners are notified only when something changed."""
        now = time.monotonic()
        with self._lock:
            current = self._snapshot
            if "phase" in changes and changes["phase"] is not current.phase:
                changes.setdefault("phase_since", now)
            if "message" in changes and changes["message"]:
                changes.setdefault("message_at", now)
            new = dataclasses.replace(current, **changes)  # type: ignore[arg-type]
            if new == current:
                return
            self._snapshot = new
            listeners = list(self._listeners)
        for listener in listeners:
            listener(new)

    def message(self, text: str) -> None:
        self.update(message=text)
