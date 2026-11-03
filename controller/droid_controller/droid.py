"""Droid connection management on top of spherov2, plus a logging fake for dry runs."""

import logging
import re
import subprocess
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import IntEnum
from functools import partial
from typing import Protocol

from spherov2.commands.animatronic import Animatronic
from spherov2.commands.sensor import CollisionDetectionMethods, Sensor
from spherov2.scanner import ToyNotFoundError, find_toys
from spherov2.sphero_edu import SpheroEduAPI, Stance
from spherov2.toy import Toy
from spherov2.toy.bb8 import BB8
from spherov2.toy.bb9e import BB9E
from spherov2.toy.r2d2 import R2D2
from spherov2.toy.r2q5 import R2Q5
from spherov2.types import Color

from .status import CRADLE, USB_CABLE, Charger
from spherov2.utils import ToyUtil

from . import spherov2_fixes
from .config import DroidConfig, parse_action

spherov2_fixes.install()

__all__ = [
    "Color",
    "Droid",
    "DroidApi",
    "DroidConnector",
    "FakeConnector",
    "PowerInfo",
    "SpheroConnector",
    "Stance",
    "resolve_action",
]

log = logging.getLogger(__name__)

DROID_TYPES: tuple[type[Toy], ...] = (R2D2, R2Q5, BB8, BB9E)
R2_TYPES: tuple[type[Toy], ...] = (R2D2, R2Q5)


class DroidApi(Protocol):
    """The subset of spherov2.sphero_edu.SpheroEduAPI this application uses."""

    def set_speed(self, speed: int) -> None: ...

    def set_heading(self, heading: int) -> None: ...

    def stop_roll(self, heading: int | None = None) -> None: ...

    def reset_aim(self) -> None: ...

    def set_main_led(self, color: Color) -> None: ...

    def set_back_led(self, color: Color | int) -> None: ...

    def set_dome_position(self, angle: float) -> None: ...

    def set_stance(self, stance: Stance) -> None: ...

    def play_animation(self, animation: IntEnum) -> None: ...

    def play_sound(self, sound: IntEnum) -> None: ...


CHARGING_STATES = ("CHARGED", "CHARGING")


@dataclass(frozen=True)
class PowerInfo:
    """What the droid says about its battery and charger."""

    state: str
    """The firmware's battery state name: CHARGED, CHARGING, NOT_CHARGING, OK, LOW, CRITICAL or UNKNOWN."""
    voltage: float | None

    @property
    def charging(self) -> bool:
        """True while the droid is on its cable or cradle; it then ignores drive and stance commands."""
        return self.state in CHARGING_STATES

    @property
    def battery(self) -> str:
        """Short battery label for the screen: the voltage when known, else the state."""
        if self.state in ("LOW", "CRITICAL"):
            return self.state
        return f"{self.voltage:.1f}V" if self.voltage is not None else self.state

    def __str__(self) -> str:
        return self.state if self.voltage is None else f"{self.state} {self.voltage:.2f} V"


@dataclass(frozen=True)
class Droid:
    """A connected droid and what it can do."""

    name: str
    api: DroidApi
    is_r2: bool
    """True for R2-D2 and R2-Q5: dome, stance and the animation buttons are available."""
    charger: Charger
    """USB cable for the R2s, charging cradle for the BB units; sets the charging warning's wording."""
    animations: type[IntEnum] | None
    sounds: type[IntEnum] | None
    play_animation: Callable[[IntEnum], None]
    """Sends the animation packet. Not api.play_animation: spherov2's Edu API passes its "wait" flag
    into the packet's target-processor slot, so R2-D2 never answers and the command times out."""
    read_power: Callable[[], PowerInfo]
    """Queries the droid; takes a couple of BLE round trips."""
    power_changed: threading.Event
    """Set by the droid's own notification when the charger or battery state changes."""


class DroidConnector(Protocol):
    def connect(self, name: str | None = None) -> AbstractContextManager[Droid]:
        """Scan and connect, to `name` if given and in range; raises if nothing suitable is found."""
        ...

    def release(self, name: str) -> None:
        """Note that the user sent this droid to sleep, so another droid is preferred next."""
        ...

    def scan_names(self, connected: str | None = None) -> list[str]:
        """Names of droids advertising right now, for the chooser; safe to call while connected."""
        ...


def _droid_for_toy(
    name: str,
    api: DroidApi,
    toy_cls: type[Toy],
    play_animation: Callable[[IntEnum], None],
    read_power: Callable[[], PowerInfo],
    power_changed: threading.Event | None = None,
) -> Droid:
    return Droid(
        name=name,
        api=api,
        is_r2=issubclass(toy_cls, R2_TYPES),
        charger=USB_CABLE if issubclass(toy_cls, R2_TYPES) else CRADLE,
        animations=getattr(toy_cls, "Animations", None),
        sounds=getattr(toy_cls, "Audio", None),
        play_animation=play_animation,
        read_power=read_power,
        power_changed=power_changed or threading.Event(),
    )


def resolve_action(droid: Droid, spec: str, label: str) -> Callable[[], None] | None:
    """Turn an "animation:NAME" or "sound:NAME" spec into a call on the droid, or None if unsupported."""
    kind, name = parse_action(spec, label)
    if kind == "animation":
        enum, call = droid.animations, droid.play_animation
    else:
        enum, call = droid.sounds, droid.api.play_sound
    member = getattr(enum, name, None) if enum is not None else None
    if member is None:
        log.warning("%s: %s %r is not available on %s, button disabled", label, kind, name, droid.name)
        return None
    return partial(call, member)


class SpheroConnector:
    """Finds a droid over BLE and connects with SpheroEduAPI."""

    def __init__(self, config: DroidConfig) -> None:
        self._config = config
        self._released: str | None = None

    def release(self, name: str) -> None:
        """The user sent this droid to sleep; prefer any other droid on the next scan."""
        self._released = name

    def scan_names(self, connected: str | None = None) -> list[str]:
        release_stale_connections(keep=connected)
        toys = find_droids(self._config.scan_timeout)
        names = [toy.name for toy in toys]
        log.info("Droids in range: %s", names or "none")
        return names

    @contextmanager
    def connect(self, name: str | None = None) -> Iterator[Droid]:
        toy = self._scan(name)
        log.info("Connecting to %s", toy)
        with SpheroEduAPI(toy) as api:
            # SpheroEduAPI turns on streaming of five sensors and does matrix maths on every
            # sample. That saturates a Pi Zero's CPU and the starved BLE stack drops the link.
            ToyUtil.disable_sensors(toy)
            disable_collision_reports(toy)
            power = read_power(toy)
            log.info("Connected to %s (sensor streaming off, battery %s)", toy, power)
            power_changed = subscribe_power_changes(toy)
            yield _droid_for_toy(
                toy.name, api, type(toy), partial(Animatronic.play_animation, toy), partial(read_power, toy), power_changed
            )
            log.info("Disconnecting from %s (droid will sleep)", toy)

    def _scan(self, wanted: str | None = None) -> Toy:
        # Nothing of ours is connected at this point, so any droid BlueZ still holds is a leftover
        # (a killed process, or a droid that woke and reattached); it will not advertise until dropped.
        release_stale_connections()
        log.info("Scanning for droids (%.0f s)%s", self._config.scan_timeout, f", looking for {wanted}" if wanted else "")
        toys = find_droids(self._config.scan_timeout)
        seen = [toy.name for toy in toys]
        if wanted is not None and wanted in seen:
            chosen: str | None = wanted
        else:
            if wanted is not None:
                log.warning("%s is not in range (saw %s); choosing as usual", wanted, seen)
            chosen = choose_droid(seen, self._config.names, self._config.name_prefixes, self._released)
        if chosen is None:
            raise ToyNotFoundError(f"no droid found (saw {[toy.name for toy in toys]})")
        if chosen == self._released:
            log.info("%s is the only droid in range; waking it again", chosen)
        self._released = None
        return next(toy for toy in toys if toy.name == chosen)


DROID_NAME = re.compile(r"^(D2|Q5|BB|GB|LM)-[0-9A-F]{4}$")
"""Sphero droid names: a model prefix and the last four hex digits of the address."""


def is_droid_name(name: str | None) -> bool:
    return bool(name) and DROID_NAME.match(name) is not None


def find_droids(timeout: float) -> list[Toy]:
    """Scan for droids, dropping anything that only looks like one.

    bleak names an unnamed BLE device by its address ("D2-C7-8C-3C-92-D9"), and spherov2's
    scanner matches by prefix alone, so a stranger's headphones can pass as an R2-D2.
    """
    toys = find_toys(timeout=timeout, toy_types=list(DROID_TYPES))
    strangers = [toy.name for toy in toys if not is_droid_name(toy.name)]
    if strangers:
        log.debug("Ignoring devices that are not droids: %s", strangers)
    return [toy for toy in toys if is_droid_name(toy.name)]


def choose_droid(
    seen: Sequence[str], names: Sequence[str], prefixes: Sequence[str], released: str | None
) -> str | None:
    """Pick which droid to connect to.

    Droids listed in `names` win, in that order. Otherwise any droid matching `prefixes`
    (or any droid when no prefixes are set). The droid most recently released with Back
    is only chosen when it is the sole candidate, so Back cycles through nearby droids.
    """
    candidates = [name for name in seen if name in names or (not names and _matches_prefix(name, prefixes))]
    candidates.sort(key=lambda name: names.index(name) if name in names else len(names))
    others = [name for name in candidates if name != released]
    if others:
        return others[0]
    return candidates[0] if candidates else None


def _matches_prefix(name: str, prefixes: Sequence[str]) -> bool:
    return not prefixes or any(name.startswith(prefix) for prefix in prefixes)


def disable_collision_reports(toy: Toy) -> None:
    """Turn off the firmware's collision detection; nothing here uses it.

    A tilted or shaken R2-D2 has been seen reporting collisions at over 70 a second, which
    swamps a Pi Zero and the BLE link. Best effort: an older droid may not accept it.
    """
    try:
        Sensor.configure_collision_detection(toy, CollisionDetectionMethods.NO_COLLISION_DETECTION, 0, 0, 0, 0, 0)
    except Exception as exc:
        log.info("%s did not accept collision detection off (%s)", toy.name, type(exc).__name__)


def read_power(toy: Toy) -> PowerInfo:
    """Ask the droid for its battery state and voltage; the generations expose different commands.

    R2-D2 and R2-Q5 answer get_battery_state with an enum and get_battery_voltage with a float.
    BB-8 speaks the older protocol: get_power_state returns one record holding both, and its
    state enum has its own numbering (there, 4 means CRITICAL).
    """
    state = "UNKNOWN"
    voltage: float | None = None
    for method in ("get_battery_state", "get_power_state", "get_battery_voltage_state"):
        query = getattr(toy, method, None)
        if query is None:
            continue
        try:
            answer = query()
        except Exception as exc:
            log.debug("%s failed: %s", method, exc)
            continue
        state = getattr(getattr(answer, "state", answer), "name", "UNKNOWN")
        voltage = getattr(answer, "voltage", None)
        break
    if voltage is None:
        query = getattr(toy, "get_battery_voltage", None)
        if query is not None:
            try:
                voltage = float(query())
            except Exception as exc:
                log.debug("get_battery_voltage failed: %s", exc)
    return PowerInfo(state, voltage)


def subscribe_power_changes(toy: Toy) -> threading.Event:
    """Have the droid notify on charger/battery changes; the returned event is set on each one.

    The listener only flags the change: it runs on the BLE reader thread, which must not
    issue commands of its own. The session re-reads the power state when it sees the flag.
    """
    changed = threading.Event()
    enable = getattr(toy, "enable_battery_state_changed_notify", None)
    add_listener = getattr(toy, "add_battery_state_changed_notify_listener", None)
    if enable is None or add_listener is None:
        log.info("%s does not report battery changes; polling instead", toy.name)
        return changed

    def on_change(state: object) -> None:
        log.debug("Battery state notification: %s", state)
        changed.set()

    try:
        add_listener(on_change)
        enable(True)
    except Exception as exc:
        log.info("%s refused battery change notifications (%s); polling instead", toy.name, type(exc).__name__)
    return changed


_BLUETOOTHCTL_DEVICE = re.compile(r"^Device ([0-9A-F:]{17}) (.*)$")


def release_stale_connections(keep: str | None = None) -> None:
    """Disconnect any droid BlueZ still holds (except `keep`); a connected droid does not advertise."""
    try:
        listing = subprocess.run(
            ["bluetoothctl", "devices", "Connected"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Could not list connected Bluetooth devices: %s", exc)
        return
    for line in listing.splitlines():
        match = _BLUETOOTHCTL_DEVICE.match(line.strip())
        if match is None or not is_droid_name(match.group(2)) or match.group(2) == keep:
            continue
        address, name = match.groups()
        log.warning("Releasing stale BlueZ connection to %s (%s)", name, address)
        subprocess.run(["bluetoothctl", "disconnect", address], capture_output=True, timeout=15, check=False)


class FakeDroidApi:
    """Logs every command instead of talking to hardware."""

    def set_speed(self, speed: int) -> None:
        log.info("fake droid: set_speed(%d)", speed)

    def set_heading(self, heading: int) -> None:
        log.info("fake droid: set_heading(%d)", heading)

    def stop_roll(self, heading: int | None = None) -> None:
        log.info("fake droid: stop_roll(%s)", heading)

    def reset_aim(self) -> None:
        log.info("fake droid: reset_aim()")

    def set_main_led(self, color: Color) -> None:
        log.info("fake droid: set_main_led(%s)", tuple(color))

    def set_back_led(self, color: Color | int) -> None:
        log.info("fake droid: set_back_led(%s)", color)

    def set_dome_position(self, angle: float) -> None:
        log.info("fake droid: set_dome_position(%s)", angle)

    def set_stance(self, stance: Stance) -> None:
        log.info("fake droid: set_stance(%s)", stance.name)

    def play_animation(self, animation: IntEnum) -> None:
        log.info("fake droid: play_animation(%s)", animation.name)

    def play_sound(self, sound: IntEnum) -> None:
        log.info("fake droid: play_sound(%s)", sound.name)

    def read_power(self) -> PowerInfo:
        return PowerInfo("NOT_CHARGING", 3.9)


class FakeConnector:
    """Connects instantly to a fake R2-D2 so every button mapping can be exercised."""

    FAKE_NAMES = ("D2-FAKE", "Q5-FAKE", "BB-FAKE")

    def release(self, name: str) -> None:
        log.info("fake droid: released %s", name)

    def scan_names(self, connected: str | None = None) -> list[str]:
        return [name for name in self.FAKE_NAMES if name != connected]

    @contextmanager
    def connect(self, name: str | None = None) -> Iterator[Droid]:
        name = name or self.FAKE_NAMES[0]
        log.info("Connected to fake droid %s", name)
        api = FakeDroidApi()
        toy_cls = R2D2 if name.startswith(("D2", "Q5")) else BB8
        yield _droid_for_toy(name, api, toy_cls, api.play_animation, api.read_power)
        log.info("Disconnected from fake droid %s (sleep requested)", name)
