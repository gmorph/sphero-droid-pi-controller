import math

import pytest

from droid_controller.mapping import (
    DOME_MAX_DEGREES,
    DOME_MIN_DEGREES,
    DriveCommand,
    aim_heading,
    apply_dead_zone,
    dome_position,
    normalise_axis,
    normalise_trigger,
    speed_from_magnitude,
    stick_heading,
    stick_to_drive,
)

DEAD_ZONE = 0.15
EXPONENT = 1.5
MAX_SPEED = 180


@pytest.mark.parametrize(
    ("x", "y", "heading"),
    [
        (0.0, -1.0, 0),  # up
        (1.0, 0.0, 90),  # right
        (0.0, 1.0, 180),  # down
        (-1.0, 0.0, 270),  # left
        (1.0, -1.0, 45),  # up-right
        (1.0, 1.0, 135),  # down-right
        (-1.0, 1.0, 225),  # down-left
        (-1.0, -1.0, 315),  # up-left
    ],
)
def test_stick_heading_cardinals_and_diagonals(x: float, y: float, heading: int) -> None:
    assert stick_heading(x, y) == heading


def test_stick_heading_is_always_in_range() -> None:
    for degrees in range(0, 360, 5):
        x = math.sin(math.radians(degrees))
        y = -math.cos(math.radians(degrees))
        assert stick_heading(x, y) == degrees


def test_normalise_axis_uses_device_range() -> None:
    assert normalise_axis(-32768, -32768, 32767) == pytest.approx(-1.0, abs=1e-4)
    assert normalise_axis(32767, -32768, 32767) == pytest.approx(1.0, abs=1e-4)
    assert normalise_axis(0, -32768, 32767) == pytest.approx(0.0, abs=1e-4)
    assert normalise_axis(127, 0, 255) == pytest.approx(0.0, abs=0.01)
    assert normalise_axis(5, 5, 5) == 0.0


def test_normalise_trigger() -> None:
    assert normalise_trigger(0, 0, 255) == 0.0
    assert normalise_trigger(255, 0, 255) == 1.0
    assert normalise_trigger(128, 0, 255) == pytest.approx(0.502, abs=0.001)
    assert normalise_trigger(300, 0, 255) == 1.0


def test_dead_zone_zeroes_small_values_and_rescales() -> None:
    assert apply_dead_zone(0.1, DEAD_ZONE) == 0.0
    assert apply_dead_zone(-0.15, DEAD_ZONE) == 0.0
    assert apply_dead_zone(1.0, DEAD_ZONE) == 1.0
    assert apply_dead_zone(-1.0, DEAD_ZONE) == -1.0
    assert apply_dead_zone(0.575, DEAD_ZONE) == pytest.approx(0.5)
    assert apply_dead_zone(0.2, 0.0) == 0.2


def test_stick_inside_dead_zone_stops_and_keeps_heading() -> None:
    assert stick_to_drive(0.05, -0.1, DEAD_ZONE, EXPONENT, MAX_SPEED, last_heading=123) == DriveCommand(123, 0)
    assert stick_to_drive(0.0, 0.0, DEAD_ZONE, EXPONENT, MAX_SPEED, last_heading=270) == DriveCommand(270, 0)


def test_speed_curve_is_monotonic_and_bounded() -> None:
    speeds = [speed_from_magnitude(m / 20, EXPONENT, MAX_SPEED) for m in range(21)]
    assert speeds == sorted(speeds)
    assert speeds[0] == 0
    assert speeds[-1] == MAX_SPEED
    assert speed_from_magnitude(0.5, EXPONENT, MAX_SPEED) == round(MAX_SPEED * 0.5**1.5)
    assert speed_from_magnitude(0.5, 1.0, MAX_SPEED) == 90
    assert speed_from_magnitude(2.0, EXPONENT, MAX_SPEED) == MAX_SPEED


def test_full_deflection_hits_max_speed_on_diagonals_too() -> None:
    assert stick_to_drive(0.0, -1.0, DEAD_ZONE, EXPONENT, MAX_SPEED, 0) == DriveCommand(0, MAX_SPEED)
    assert stick_to_drive(1.0, 1.0, DEAD_ZONE, EXPONENT, MAX_SPEED, 0) == DriveCommand(135, MAX_SPEED)
    assert stick_to_drive(0.0, -1.0, DEAD_ZONE, EXPONENT, 255, 0).speed == 255


def test_partial_deflection_uses_curve_after_dead_zone() -> None:
    command = stick_to_drive(0.0, -0.575, DEAD_ZONE, EXPONENT, MAX_SPEED, 0)
    assert command == DriveCommand(0, round(MAX_SPEED * 0.5**1.5))


def test_aim_heading_turns_at_rate_and_wraps() -> None:
    assert aim_heading(0.0, 1.0, DEAD_ZONE, 90.0, 0.5) == pytest.approx(45.0)
    assert aim_heading(10.0, -1.0, DEAD_ZONE, 90.0, 0.5) == pytest.approx(325.0)
    assert aim_heading(100.0, 0.1, DEAD_ZONE, 90.0, 1.0) == pytest.approx(100.0)


def test_dome_position_spans_supported_range() -> None:
    assert dome_position(0.0, DEAD_ZONE) == 0
    assert dome_position(0.1, DEAD_ZONE) == 0
    assert dome_position(1.0, DEAD_ZONE) == DOME_MAX_DEGREES
    assert dome_position(-1.0, DEAD_ZONE) == DOME_MIN_DEGREES
    assert 0 < dome_position(0.5, DEAD_ZONE) < DOME_MAX_DEGREES
