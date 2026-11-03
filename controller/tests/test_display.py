import time

import pytest

from droid_controller.display import (
    XPAD_OFF,
    XPAD_PLAYER_1,
    XPAD_ROTATING,
    XPAD_SLOW_BLINK,
    Fonts,
    animated,
    led_pattern,
    render,
)
from droid_controller.status import CRADLE, USB_CABLE, Charger, Phase, Status, StatusSnapshot

SIZE = (128, 64)


@pytest.fixture(scope="module")
def fonts() -> Fonts:
    return Fonts.load()


@pytest.mark.parametrize("phase", list(Phase))
def test_render_every_phase_fills_the_screen_size(fonts: Fonts, phase: Phase) -> None:
    now = time.monotonic()
    snapshot = StatusSnapshot(
        phase=phase, phase_since=now - 1.5, attempt=3, droid="D2-F5CA", battery="OK", gamepad=True,
        heading=225, speed=180, aiming=True, stance="Bipod", led_on=False, message="EMOTE_YES", message_at=now,
    )
    image = render(snapshot, now, SIZE, fonts, ip="10.2.1.20")
    assert image.size == SIZE
    assert image.mode == "1"
    assert image.getbbox() is not None, "nothing was drawn"


def test_connected_screen_shows_more_than_idle_screen(fonts: Fonts) -> None:
    now = time.monotonic()
    idle = render(StatusSnapshot(phase=Phase.CONNECTED, droid="D2-F5CA"), now, SIZE, fonts)
    busy = render(StatusSnapshot(phase=Phase.CONNECTED, droid="D2-F5CA", speed=255, gamepad=True), now, SIZE, fonts)
    assert _lit(busy) > _lit(idle)  # the speed bar fills in


def _lit(image) -> int:
    return image.convert("L").histogram()[255]


def test_facts_row_never_runs_into_the_compass(fonts: Fonts) -> None:
    now = time.monotonic()
    snapshot = StatusSnapshot(phase=Phase.CONNECTED, droid="Q5-B8D7", battery="UNKNOWN", stance="Tripod", led_on=False)
    image = render(snapshot, now, SIZE, fonts)
    # The gap column just left of the compass, at the facts row, must stay empty.
    strip = image.crop((SIZE[0] - 26, 19, SIZE[0] - 23, 31))
    assert _lit(strip) == 0


def test_message_fades_out(fonts: Fonts) -> None:
    now = time.monotonic()
    fresh = StatusSnapshot(phase=Phase.CONNECTED, droid="D2-F5CA", message="Aim reset", message_at=now)
    stale = StatusSnapshot(phase=Phase.CONNECTED, droid="D2-F5CA", message="Aim reset", message_at=now - 60)
    assert render(fresh, now, SIZE, fonts).tobytes() != render(stale, now, SIZE, fonts).tobytes()


@pytest.mark.parametrize("charger", [USB_CABLE, CRADLE], ids=lambda c: c.tag)
def test_charging_shows_a_standing_warning(fonts: Fonts, charger: Charger) -> None:
    now = time.monotonic()
    off = StatusSnapshot(phase=Phase.CONNECTED, droid="Q5-B8D7", battery="4.1V", charger=charger)
    on = StatusSnapshot(phase=Phase.CONNECTED, droid="Q5-B8D7", battery="4.1V", charging=True, charger=charger)
    bottom = (0, SIZE[1] - 12, SIZE[0], SIZE[1])
    assert _lit(render(off, now, SIZE, fonts).crop(bottom)) == 0
    assert _lit(render(on, now, SIZE, fonts).crop(bottom)) > SIZE[0] * 6  # an inverted bar, not just text
    assert _lit(render(on, now + 3600, SIZE, fonts).crop(bottom)) > SIZE[0] * 6  # and it does not fade


@pytest.mark.parametrize("charger", [USB_CABLE, CRADLE], ids=lambda c: c.tag)
def test_charging_warning_fits_the_screen(fonts: Fonts, charger: Charger) -> None:
    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(Image.new("1", SIZE))
    assert draw.textlength(charger.warning, font=fonts.body) <= SIZE[0] - 2
    assert draw.textlength(f"{charger.tag}  Batt 7.7V", font=fonts.body) <= SIZE[0] - 26  # left of the compass


def test_animated_frames() -> None:
    now = time.monotonic()
    assert animated(StatusSnapshot(phase=Phase.SCANNING), now)
    assert animated(StatusSnapshot(phase=Phase.CONNECTED, message="Aim reset", message_at=now), now)
    assert not animated(StatusSnapshot(phase=Phase.CONNECTED, message="Aim reset", message_at=now - 60), now)
    assert not animated(StatusSnapshot(phase=Phase.CONNECTED), now)


def test_led_pattern_follows_phase() -> None:
    assert led_pattern(StatusSnapshot(phase=Phase.STARTING)) == XPAD_ROTATING
    assert led_pattern(StatusSnapshot(phase=Phase.SCANNING)) == XPAD_ROTATING
    assert led_pattern(StatusSnapshot(phase=Phase.CONNECTED)) == XPAD_PLAYER_1
    assert led_pattern(StatusSnapshot(phase=Phase.SLEEPING)) == XPAD_SLOW_BLINK
    assert led_pattern(StatusSnapshot(phase=Phase.STOPPED)) == XPAD_OFF


def test_status_notifies_only_on_change() -> None:
    status = Status()
    seen: list[StatusSnapshot] = []
    status.add_listener(seen.append)
    assert len(seen) == 1  # the current snapshot on subscription
    status.update(gamepad=True)
    status.update(gamepad=True)
    assert len(seen) == 2
    assert seen[-1].gamepad is True


def test_status_stamps_phase_and_message_times() -> None:
    status = Status()
    before = time.monotonic()
    status.update(phase=Phase.SCANNING, message="Retry in 2 s")
    snapshot = status.snapshot()
    assert snapshot.phase_since >= before
    assert snapshot.message_at >= before
    status.update(phase=Phase.SCANNING, attempt=2)
    assert status.snapshot().phase_since == snapshot.phase_since  # same phase, time kept
