"""Session behaviour that bit in the field, driven through a recording droid and a canned gamepad."""

import threading
import time
from enum import IntEnum

from droid_controller.app import Session, SessionEnd
from droid_controller.config import Config
from droid_controller.droid import Droid, PowerInfo, Stance
from droid_controller.gamepad import CENTRED_AXES, GamepadState
from droid_controller.status import USB_CABLE


class RecordingApi:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        def record(*args) -> None:
            self.calls.append(name)
        return record


class CannedGamepad:
    def __init__(self) -> None:
        self.axes = dict(CENTRED_AXES)
        self.presses: list[str] = []

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def snapshot(self) -> GamepadState:
        return GamepadState(True, dict(self.axes), time.monotonic())

    def pop_presses(self) -> list[str]:
        presses, self.presses = self.presses, []
        return presses


class NoAnimations(IntEnum):
    pass


def make_session(seen: list[str] | None = None) -> tuple[Session, RecordingApi, CannedGamepad]:
    api = RecordingApi()
    droid = Droid(
        name="D2-TEST", api=api, is_r2=True, charger=USB_CABLE, animations=NoAnimations, sounds=NoAnimations,
        play_animation=lambda a: None, read_power=lambda: PowerInfo("OK", 3.9), power_changed=threading.Event(),
    )
    gamepad = CannedGamepad()
    session = Session(Config(), gamepad, droid, threading.Event(), threading.Event(), scan=lambda: list(seen or []))
    session._setup()
    api.calls.clear()
    return session, api, gamepad


def test_bipod_silences_drive_traffic_and_the_heartbeat() -> None:
    session, api, gamepad = make_session()
    session._handle_presses(["lb"])
    assert api.calls == ["set_stance"]
    now = time.monotonic()
    session._update_motion(gamepad.snapshot(), now)
    session._heartbeat(now + 100)
    session._send_stop()
    assert api.calls == ["set_stance"], "a roll or stop command would drop the droid back to tripod"


def test_moving_the_stick_in_bipod_drops_to_tripod_then_drives() -> None:
    session, api, gamepad = make_session()
    session._handle_presses(["lb"])
    gamepad.axes["ly"] = -1.0
    session._update_motion(gamepad.snapshot(), time.monotonic() + 1)
    assert api.calls[:2] == ["set_stance", "set_stance"]
    assert "set_speed" in api.calls
    assert session._stance is Stance.Tripod


def test_repeated_stance_press_is_not_resent() -> None:
    session, api, _ = make_session()
    session._handle_presses(["rb"])  # already in tripod after connecting
    assert api.calls == []
    session._handle_presses(["lb", "lb"])
    assert api.calls == ["set_stance"]


def open_menu(session: Session, gamepad: CannedGamepad) -> None:
    session._open_menu()
    for _ in range(50):  # the scan runs in a thread; it is instant here
        if not session._menu_results.empty():
            break
        time.sleep(0.01)
    session._menu_tick(gamepad.snapshot(), [])


def test_back_opens_a_menu_listing_this_droid_the_scan_results_and_sleep() -> None:
    session, api, gamepad = make_session(seen=["Q5-B8D7", "BB-E13C"])
    open_menu(session, gamepad)
    assert api.calls == ["stop_roll"]
    snapshot = session._status.snapshot()
    assert snapshot.menu_items == ("D2-TEST (this one)", "Q5-B8D7", "BB-E13C", "Sleep this droid")
    assert not snapshot.menu_scanning


def test_choosing_another_droid_ends_the_session_with_its_name() -> None:
    session, _, gamepad = make_session(seen=["Q5-B8D7"])
    open_menu(session, gamepad)
    assert session._menu_tick(gamepad.snapshot(), ["dpad_down"]) is None
    assert session._menu_tick(gamepad.snapshot(), ["a"]) is SessionEnd.SWITCH
    assert session.switch_to == "Q5-B8D7"


def test_stick_steps_once_per_push_and_sleep_item_sleeps() -> None:
    session, _, gamepad = make_session(seen=[])
    open_menu(session, gamepad)
    gamepad.axes["ly"] = 1.0
    session._menu_tick(gamepad.snapshot(), [])
    session._menu_tick(gamepad.snapshot(), [])  # still held: no extra step
    assert session._status.snapshot().menu_index == 1
    assert session._menu_tick(gamepad.snapshot(), ["a"]) is SessionEnd.SLEEP


def test_b_cancels_and_driving_resumes() -> None:
    session, api, gamepad = make_session(seen=[])
    open_menu(session, gamepad)
    assert session._menu_tick(gamepad.snapshot(), ["b"]) is None
    assert session._menu is None
    assert session._status.snapshot().menu_items == ()
    gamepad.axes["ly"] = -1.0
    session._update_motion(gamepad.snapshot(), time.monotonic() + 1)
    assert "set_speed" in api.calls
