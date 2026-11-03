"""The control loop: gamepad state in, droid commands out, reconnect forever."""

import logging
import queue
import threading
import time
from collections.abc import Callable
from enum import Enum, auto

from .config import R2_ACTION_BUTTONS, Config
from .droid import Color, Droid, DroidConnector, PowerInfo, Stance, resolve_action
from .gamepad import GamepadSource, GamepadState
from .mapping import DriveCommand, aim_heading, dome_position, stick_to_drive
from .menu import SLEEP_ITEM, DroidMenu
from .status import Phase, Status

log = logging.getLogger(__name__)

LED_OFF = Color(0, 0, 0)
BACK_LED_AIM_BRIGHTNESS = 255
POWER_POLL_INTERVAL = 30.0  # seconds; the droid also notifies on change, this catches a missed one


class SessionEnd(Enum):
    STOP = auto()
    SLEEP = auto()
    SWITCH = auto()
    """The user chose another droid from the menu; Session.switch_to names it."""


MENU_STICK_THRESHOLD = 0.6


class Controller:
    """Connects to a droid, drives it until it goes away, then reconnects with backoff."""

    def __init__(
        self,
        config: Config,
        gamepad: GamepadSource,
        connector: DroidConnector,
        stop: threading.Event,
        wake: threading.Event,
        status: Status | None = None,
    ) -> None:
        self._config = config
        self._gamepad = gamepad
        self._connector = connector
        self._stop = stop
        self._wake = wake
        self._status = status or Status()

    def run(self) -> None:
        """Return only when stop is set."""
        droid_config = self._config.droid
        backoff = droid_config.reconnect_backoff_min
        attempt = 0
        wanted: str | None = None
        while not self._stop.is_set():
            attempt += 1
            log.info("Looking for a droid (attempt %d)", attempt)
            self._status.update(phase=Phase.SCANNING, attempt=attempt)
            end = None
            try:
                with self._connector.connect(wanted) as droid:
                    attempt = 0
                    backoff = droid_config.reconnect_backoff_min
                    wanted = None
                    session = Session(
                        self._config, self._gamepad, droid, self._stop, self._wake, self._status,
                        scan=lambda: self._connector.scan_names(droid.name),
                    )
                    end = session.run()
                    if end in (SessionEnd.SLEEP, SessionEnd.SWITCH):
                        # Released here, not after the block: a droid drops the link as it
                        # goes to sleep, so the disconnect that follows often raises.
                        self._connector.release(droid.name)
                        wanted = session.switch_to
                log.info("Disconnected from %s", droid.name)
            except Exception as exc:
                if end in (SessionEnd.SLEEP, SessionEnd.SWITCH):
                    log.info("Link closed while the droid went to sleep (%s)", type(exc).__name__)
                else:
                    log.error("Droid connection ended: %s: %s", type(exc).__name__, exc)
                    log.debug("Traceback", exc_info=True)
                    log.info("Retrying in %.0f s", backoff)
                    self._status.update(phase=Phase.SCANNING, message=f"Retry in {backoff:.0f} s")
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, droid_config.reconnect_backoff_max)
                    continue
            if end in (SessionEnd.SLEEP, SessionEnd.SWITCH):
                log.info("Looking for %s in %.0f s", wanted or "another droid", droid_config.sleep_rescan_delay)
                self._status.update(phase=Phase.SLEEPING)
                self._stop.wait(droid_config.sleep_rescan_delay)
        self._status.update(phase=Phase.STOPPED, droid=None)


class Session:
    """Drives one connected droid. Any droid error propagates to the Controller, which reconnects."""

    def __init__(
        self,
        config: Config,
        gamepad: GamepadSource,
        droid: Droid,
        stop: threading.Event,
        wake: threading.Event,
        status: Status | None = None,
        scan: Callable[[], list[str]] | None = None,
    ) -> None:
        self._config = config
        self._gamepad = gamepad
        self._droid = droid
        self._api = droid.api
        self._stop = stop
        self._wake = wake
        self._status = status or Status()
        self._scan = scan or (lambda: [])
        self._menu: DroidMenu | None = None
        self._menu_results: queue.SimpleQueue[list[str]] = queue.SimpleQueue()
        self._menu_stick = 0
        self.switch_to: str | None = None
        self._interval = 1.0 / config.drive.update_rate
        self._heading = 0.0
        self._last_sent: DriveCommand | None = None
        self._last_drive_at = 0.0
        self._aiming = False
        self._aim_at = 0.0
        self._led_on = True
        self._last_dome = 0
        self._last_dome_at = 0.0
        self._safety_stopped = False
        self._actions: dict[str, Callable[[], None]] = {}
        self._power: PowerInfo | None = None
        self._power_read_at = 0.0
        self._stance: Stance | None = None
        """Current R2 stance. Any roll or stop command drops the droid into tripod, so while in
        bipod no drive traffic (not even the heartbeat) is sent until the stick asks for motion."""

    def run(self) -> SessionEnd:
        self._setup()
        next_tick = time.monotonic()
        while True:
            if self._stop.is_set():
                return self._finish(SessionEnd.STOP)
            now = time.monotonic()
            state = self._gamepad.snapshot()
            presses = self._gamepad.pop_presses()
            if not self._input_unavailable(state, now):
                if self._menu is not None:
                    end = self._menu_tick(state, presses)
                    if end is not None:
                        return self._finish(end)
                elif "back" in presses:
                    self._open_menu()
                else:
                    self._handle_presses(presses)
                    self._update_motion(state, now)
                    self._update_dome(state, now)
            self._heartbeat(now)
            self._update_power(now)
            next_tick = self._sleep_until(next_tick + self._interval)

    def _setup(self) -> None:
        stale = self._gamepad.pop_presses()  # presses made while no droid was connected must not replay now
        if stale:
            log.info("Discarding %d button press(es) made while disconnected", len(stale))
        self._stance = Stance.Tripod if self._droid.is_r2 else None  # the connect sequence sets tripod
        self._status.update(
            phase=Phase.CONNECTED, droid=self._droid.name, charger=self._droid.charger, heading=0, speed=0,
            aiming=False, stance=self._stance.name if self._stance else None, led_on=self._led_on, message="",
        )
        self._update_power(time.monotonic(), force=True)
        self._api.set_main_led(self._main_led_color())
        log.info("Main LED on")
        if self._droid.is_r2:
            for button in R2_ACTION_BUTTONS:
                spec = getattr(self._config.r2, button)
                action = resolve_action(self._droid, spec, f"[r2] {button}")
                if action is not None:
                    self._actions[button] = action
            log.info("%s: dome, stance and %d action buttons enabled", self._droid.name, len(self._actions))
        else:
            log.info("%s: no dome, stance or action buttons on this droid", self._droid.name)

    def _finish(self, end: SessionEnd) -> SessionEnd:
        self._send_stop()
        self._status.update(menu_items=(), menu_index=0, menu_scanning=False)
        return end

    # -- the droid chooser -------------------------------------------------------------------

    def _open_menu(self) -> None:
        log.info("Back pressed: opening the droid menu")
        self._send_stop()
        self._menu = DroidMenu(self._droid.name)
        self._menu_stick = 0
        self._publish_menu()
        threading.Thread(target=self._scan_for_menu, name="menu-scan", daemon=True).start()

    def _scan_for_menu(self) -> None:
        try:
            names = self._scan()
        except Exception as exc:
            log.warning("Scan for the menu failed: %s: %s", type(exc).__name__, exc)
            names = []
        self._menu_results.put(names)
        self._wake.set()

    def _close_menu(self) -> None:
        self._menu = None
        self._status.update(menu_items=(), menu_index=0, menu_scanning=False)

    def _publish_menu(self) -> None:
        menu = self._menu
        if menu is not None:
            self._status.update(menu_items=tuple(menu.labels()), menu_index=menu.index, menu_scanning=menu.scanning)

    def _menu_tick(self, state: GamepadState, presses: list[str]) -> SessionEnd | None:
        menu = self._menu
        assert menu is not None
        try:
            while True:
                menu.add_seen(self._menu_results.get_nowait())
        except queue.Empty:
            pass
        steps = presses.count("dpad_down") - presses.count("dpad_up") + self._menu_stick_step(state)
        if steps:
            menu.move(steps)
        if "a" in presses:
            choice = menu.selected
            if choice == SLEEP_ITEM:
                log.info("Menu: putting %s to sleep", self._droid.name)
                return SessionEnd.SLEEP
            if choice == self._droid.name:
                log.info("Menu: staying on %s", choice)
                self._close_menu()
                return None
            log.info("Menu: switching to %s", choice)
            self.switch_to = choice
            return SessionEnd.SWITCH
        if "b" in presses or "back" in presses:
            log.info("Menu closed")
            self._close_menu()
            return None
        self._publish_menu()
        return None

    def _menu_stick_step(self, state: GamepadState) -> int:
        """One step per push of the left stick up or down; it must return to centre between steps."""
        y = state.axes["ly"]
        zone = -1 if y < -MENU_STICK_THRESHOLD else 1 if y > MENU_STICK_THRESHOLD else 0
        step = zone if zone != self._menu_stick else 0
        self._menu_stick = zone
        return step

    def _sleep_until(self, next_tick: float) -> float:
        """Wait for the next tick or an early wake-up (button press, stick centred); returns the tick time."""
        remaining = next_tick - time.monotonic()
        if remaining <= 0:
            return time.monotonic()
        if self._wake.wait(timeout=remaining):
            self._wake.clear()
        return next_tick

    def _input_unavailable(self, state: GamepadState, now: float) -> bool:
        """Stop the droid once when the gamepad is gone or silent for too long."""
        idle_timeout = self._config.gamepad.idle_timeout
        if state.connected and (idle_timeout == 0 or now - state.last_event < idle_timeout):
            self._safety_stopped = False
            return False
        if not self._safety_stopped:
            reason = "gamepad disconnected" if not state.connected else f"no gamepad input for {idle_timeout:.0f} s"
            log.warning("%s: stopping droid", reason)
            self._send_stop()
            self._status.message("Stopped: " + ("no gamepad" if not state.connected else "idle"))
            self._safety_stopped = True
        return True

    def _handle_presses(self, presses: list[str]) -> None:
        for button in presses:
            if button == "start":
                self._toggle_led()
            elif button == "guide":
                log.info("Guide pressed: stop and reset aim")
                self._send_stop()
                self._reset_aim()
            elif button in ("lb", "rb"):
                self._set_stance(Stance.Bipod if button == "lb" else Stance.Tripod)
            elif button in self._actions:
                spec = getattr(self._config.r2, button)
                log.info("Button %s: %s", button, spec)
                self._status.message(spec.partition(":")[2])
                self._actions[button]()
                if self._last_sent is not None and self._last_sent.speed != 0:
                    self._last_sent = None  # animations stop the droid; resend the drive state next tick
            else:
                log.debug("Button %s: nothing mapped on %s", button, self._droid.name)

    def _toggle_led(self) -> None:
        self._led_on = not self._led_on
        self._api.set_main_led(self._main_led_color())
        log.info("Main LED %s", "on" if self._led_on else "off")
        self._status.update(led_on=self._led_on)

    def _main_led_color(self) -> Color:
        return Color(*self._config.droid.main_led) if self._led_on else LED_OFF

    def _set_stance(self, stance: Stance) -> None:
        if not self._droid.is_r2:
            log.debug("Stance ignored on %s", self._droid.name)
            return
        if stance is self._stance:
            return
        log.info("Stance: %s", stance.name)
        self._api.set_stance(stance)
        self._stance = stance
        self._status.update(stance=stance.name)

    def _update_motion(self, state: GamepadState, now: float) -> None:
        aim_held = state.axes["lt"] > self._config.drive.aim_trigger_threshold
        if self._stance is Stance.Bipod:
            if not aim_held and self._stick_command(state).speed == 0:
                return  # standing on two legs: stay quiet so the droid stays up
            log.info("Stick moved in bipod: dropping to tripod to drive")
            self._set_stance(Stance.Tripod)
        if aim_held and not self._aiming:
            self._begin_aim(now)
        elif self._aiming and not aim_held:
            self._end_aim()
        if self._aiming:
            self._update_aim(state, now)
        else:
            self._update_drive(state, now)

    def _begin_aim(self, now: float) -> None:
        log.info("Aim mode on")
        self._send_stop()
        self._api.set_back_led(BACK_LED_AIM_BRIGHTNESS)
        self._aiming = True
        self._aim_at = now
        self._status.update(aiming=True)

    def _end_aim(self) -> None:
        log.info("Aim mode off")
        self._aiming = False
        self._reset_aim()
        self._api.set_back_led(0)
        self._api.set_main_led(self._main_led_color())
        self._status.update(aiming=False)

    def _update_aim(self, state: GamepadState, now: float) -> None:
        drive = self._config.drive
        self._heading = aim_heading(
            self._heading, state.axes["lx"], self._config.gamepad.dead_zone, drive.aim_turn_rate, now - self._aim_at
        )
        self._aim_at = now
        self._send_drive(DriveCommand(heading=round(self._heading) % 360, speed=0), now)

    def _update_drive(self, state: GamepadState, now: float) -> None:
        command = self._stick_command(state)
        self._heading = float(command.heading)
        self._send_drive(command, now)

    def _stick_command(self, state: GamepadState) -> DriveCommand:
        drive = self._config.drive
        return stick_to_drive(
            state.axes["lx"],
            state.axes["ly"],
            self._config.gamepad.dead_zone,
            drive.speed_exponent,
            drive.max_speed,
            round(self._heading) % 360,
        )

    def _send_drive(self, command: DriveCommand, now: float) -> None:
        """Send a changed drive command, rate limited except for stops, which go out immediately."""
        last = self._last_sent
        if command == last:
            return
        if command.speed == 0 and (last is None or last.speed != 0):
            log.debug("stop_roll(%d)", command.heading)
            self._api.stop_roll(self._out(command.heading))
        elif now - self._last_drive_at >= self._interval:
            if last is None or command.heading != last.heading:
                log.debug("set_heading(%d)", command.heading)
                self._api.set_heading(self._out(command.heading))
            if last is None or command.speed != last.speed:
                log.debug("set_speed(%d)", command.speed)
                self._api.set_speed(command.speed)
        else:
            return
        self._last_sent = command
        self._last_drive_at = now
        self._status.update(heading=command.heading, speed=command.speed)

    def _send_stop(self) -> None:
        heading = round(self._heading) % 360
        if self._stance is Stance.Bipod:
            log.debug("stop skipped in bipod (a stop command would drop the droid to tripod)")
        else:
            log.debug("stop_roll(%d)", heading)
            self._api.stop_roll(self._out(heading))
        self._last_sent = DriveCommand(heading=heading, speed=0)
        self._last_drive_at = time.monotonic()
        self._status.update(heading=heading, speed=0)

    def _reset_aim(self) -> None:
        """Make the droid's current orientation heading 0 and resync the API's heading to match."""
        log.info("Aim reset")
        self._api.reset_aim()
        self._api.stop_roll(self._out(0))
        self._heading = 0.0
        self._last_sent = DriveCommand(heading=0, speed=0)
        self._last_drive_at = time.monotonic()
        self._status.update(heading=0, speed=0, message="Aim reset")

    def _out(self, heading: int) -> int:
        """Heading as sent to the droid: the stick heading plus the configured fixed offset."""
        return (heading + self._config.drive.heading_offset) % 360

    def _heartbeat(self, now: float) -> None:
        """Re-send the current drive state while idle so a vanished droid is noticed promptly."""
        interval = self._config.drive.heartbeat_interval
        last = self._last_sent
        if interval == 0 or last is None or now - self._last_drive_at < interval:
            return
        if self._stance is Stance.Bipod:
            self._last_drive_at = now  # the periodic power read keeps checking the link instead
            return
        log.debug("heartbeat: resend %s", last)
        if last.speed == 0:
            self._api.stop_roll(self._out(last.heading))
        else:
            self._api.set_speed(last.speed)
        self._last_drive_at = now

    def _update_power(self, now: float, force: bool = False) -> None:
        """Re-read battery and charger state when the droid flags a change, or every POWER_POLL_INTERVAL."""
        if not (force or self._droid.power_changed.is_set() or now - self._power_read_at >= POWER_POLL_INTERVAL):
            return
        self._droid.power_changed.clear()
        power = self._droid.read_power()
        self._power_read_at = time.monotonic()
        if power != self._power:
            if self._power is None or power.charging != self._power.charging:
                if power.charging:
                    log.warning(
                        "%s is on its %s: it will not drive or change stance while charging",
                        self._droid.name, self._droid.charger.name,
                    )
                else:
                    log.info("%s is off its %s (battery %s)", self._droid.name, self._droid.charger.name, power)
            self._power = power
            self._status.update(battery=power.battery, charging=power.charging)

    def _update_dome(self, state: GamepadState, now: float) -> None:
        if not self._droid.is_r2:
            return
        position = dome_position(state.axes["rx"], self._config.gamepad.dead_zone)
        if position == self._last_dome or now - self._last_dome_at < self._interval:
            return
        log.debug("set_dome_position(%d)", position)
        self._api.set_dome_position(position)
        self._last_dome = position
        self._last_dome_at = now
