"""Pure stick-to-command maths. No I/O, no state.

Axis convention follows evdev: x is -1.0 (left) to 1.0 (right) and y is
-1.0 (up) to 1.0 (down).
"""

import math
from dataclasses import dataclass

DOME_MIN_DEGREES = -160.0
DOME_MAX_DEGREES = 180.0


@dataclass(frozen=True)
class DriveCommand:
    heading: int
    speed: int


def normalise_axis(raw: int, minimum: int, maximum: int) -> float:
    """Scale a raw absolute axis value to -1.0..1.0 around the range midpoint."""
    if maximum <= minimum:
        return 0.0
    half_range = (maximum - minimum) / 2.0
    centre = minimum + half_range
    return max(-1.0, min(1.0, (raw - centre) / half_range))


def normalise_trigger(raw: int, minimum: int, maximum: int) -> float:
    """Scale a raw trigger value to 0.0 (released) .. 1.0 (fully pressed)."""
    if maximum <= minimum:
        return 0.0
    return max(0.0, min(1.0, (raw - minimum) / (maximum - minimum)))


def apply_dead_zone(value: float, dead_zone: float) -> float:
    """Zero a magnitude inside the dead zone and rescale the rest to 0.0..1.0."""
    magnitude = abs(value)
    if magnitude <= dead_zone:
        return 0.0
    scaled = (magnitude - dead_zone) / (1.0 - dead_zone)
    return math.copysign(min(1.0, scaled), value)


def stick_heading(x: float, y: float) -> int:
    """Heading in degrees for a stick deflection: up is 0, right is 90, down is 180, left is 270."""
    return round(math.degrees(math.atan2(x, -y))) % 360


def speed_from_magnitude(magnitude: float, exponent: float, max_speed: int) -> int:
    """Map a 0.0..1.0 stick magnitude to 0..max_speed through a power curve."""
    clamped = max(0.0, min(1.0, magnitude))
    return round(max_speed * clamped**exponent)


def stick_to_drive(
    x: float, y: float, dead_zone: float, exponent: float, max_speed: int, last_heading: int
) -> DriveCommand:
    """Convert a left-stick deflection to a heading and speed.

    Inside the (radial) dead zone the speed is 0 and the previous heading is
    kept so the droid does not spin when the stick is released.
    """
    magnitude = apply_dead_zone(min(1.0, math.hypot(x, y)), dead_zone)
    if magnitude == 0.0:
        return DriveCommand(heading=last_heading, speed=0)
    return DriveCommand(
        heading=stick_heading(x, y),
        speed=speed_from_magnitude(magnitude, exponent, max_speed),
    )


def aim_heading(heading: float, x: float, dead_zone: float, turn_rate: float, dt: float) -> float:
    """Advance the aim heading by stick x deflection over dt seconds at turn_rate degrees/s."""
    return (heading + apply_dead_zone(x, dead_zone) * turn_rate * dt) % 360.0


def dome_position(x: float, dead_zone: float) -> int:
    """Map stick x to the R2 dome range, left to DOME_MIN_DEGREES and right to DOME_MAX_DEGREES."""
    deflection = apply_dead_zone(x, dead_zone)
    limit = DOME_MAX_DEGREES if deflection >= 0 else -DOME_MIN_DEGREES
    return round(deflection * limit)
