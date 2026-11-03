"""Status feedback hardware: a small I2C OLED screen and the Xbox 360 LED ring.

Both are optional. `OledDisplay.open` returns None, with a warning, when the screen
is not reachable, so the controller works unchanged without one. Rendering is a
pure function of a StatusSnapshot (see `render`) so it can be tested without
hardware; the drawing runs in its own thread so the slow I2C transfer never
delays a drive command.
"""

import glob
import logging
import math
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from PIL import Image, ImageDraw, ImageFont

from .config import DisplayConfig
from .status import Phase, Status, StatusSnapshot

log = logging.getLogger(__name__)

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
IP_REFRESH = 10.0
MESSAGE_LIFETIME = 4.0


@dataclass(frozen=True)
class Fonts:
    title: ImageFont.FreeTypeFont | ImageFont.ImageFont
    body: ImageFont.FreeTypeFont | ImageFont.ImageFont

    @classmethod
    def load(cls) -> "Fonts":
        try:
            return cls(
                title=ImageFont.truetype(f"{FONT_DIR}/DejaVuSans-Bold.ttf", 13),
                body=ImageFont.truetype(f"{FONT_DIR}/DejaVuSans.ttf", 10),
            )
        except OSError:
            log.warning("DejaVu fonts not found; using the built-in font")
            return cls(title=ImageFont.load_default(13), body=ImageFont.load_default(10))


class Screen(Protocol):
    """What this module needs from a luma.oled device."""

    size: tuple[int, int]

    def display(self, image: Image.Image) -> None: ...


def open_screen(config: DisplayConfig) -> Screen | None:
    """Open the OLED, or return None (after logging why) when there is no usable screen."""
    try:
        from luma.core.interface.serial import i2c
        from luma.oled import device as oled
    except ImportError as exc:
        log.warning("Status screen disabled: luma.oled is not installed (%s)", exc)
        return None
    try:
        serial = i2c(port=config.i2c_port, address=config.i2c_address)
        driver = getattr(oled, config.driver)
        screen = driver(serial, width=config.width, height=config.height, rotate=config.rotate, persist=True)
        screen.contrast(config.contrast)
    except Exception as exc:
        log.warning(
            "Status screen disabled: no %s at I2C port %d address 0x%02X (%s)",
            config.driver, config.i2c_port, config.i2c_address, exc,
        )
        return None
    log.info("Status screen: %s %dx%d at I2C 0x%02X", config.driver, config.width, config.height, config.i2c_address)
    return screen


class OledDisplay:
    """Renders the status onto the screen whenever it changes, at most refresh_rate times a second."""

    def __init__(self, config: DisplayConfig, status: Status, screen: Screen) -> None:
        self._config = config
        self._status = status
        self._screen = screen
        self._fonts = Fonts.load()
        self._changed = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="display", daemon=True)

    def start(self) -> None:
        self._status.add_listener(lambda _snapshot: self._changed.set())
        self._thread.start()

    def stop(self) -> None:
        """Show the stopped screen and leave it there; a stopped service should look stopped."""
        self._stop.set()
        self._changed.set()
        self._thread.join(timeout=2.0)
        self._draw(self._status.snapshot(), time.monotonic())

    def _run(self) -> None:
        """Redraw when the status changed or the frame is animated, never faster than refresh_rate."""
        interval = 1.0 / self._config.refresh_rate
        last_drawn = 0.0
        dirty = True
        ip = ""
        ip_at = 0.0
        while not self._stop.is_set():
            if self._changed.wait(timeout=interval):
                self._changed.clear()
                dirty = True
            now = time.monotonic()
            snapshot = self._status.snapshot()
            if not (dirty or animated(snapshot, now)) or now - last_drawn < interval:
                continue
            if snapshot.phase is Phase.SCANNING and now - ip_at >= IP_REFRESH:
                ip, ip_at = local_ip(), now
            self._draw(snapshot, now, ip)
            last_drawn = now
            dirty = False

    def _draw(self, snapshot: StatusSnapshot, now: float, ip: str = "") -> None:
        try:
            self._screen.display(render(snapshot, now, self._screen.size, self._fonts, ip))
        except Exception as exc:
            log.warning("Status screen write failed: %s", exc)


def render(
    snapshot: StatusSnapshot, now: float, size: tuple[int, int], fonts: Fonts, ip: str = ""
) -> Image.Image:
    """Draw one frame. Layout for 128x64; other sizes scale the compass and keep the text rows."""
    width, height = size
    image = Image.new("1", size, 0)
    draw = ImageDraw.Draw(image)
    phase = snapshot.phase

    # Row 1 (the yellow band on two-colour modules): who we are talking to, and the gamepad.
    title = {
        Phase.STARTING: "Starting",
        Phase.SCANNING: "Searching" + "." * (int((now - snapshot.phase_since) * 2) % 4),
        Phase.CONNECTED: "Choose droid" if snapshot.menu_items else snapshot.droid or "Connected",
        Phase.SLEEPING: "Sleeping",
        Phase.STOPPED: "Stopped",
    }[phase]
    draw.text((0, 0), title, font=fonts.title, fill=1)
    pad = "PAD" if snapshot.gamepad else "no pad"
    draw.text((width - _text_width(draw, pad, fonts.body) - 1, 2), pad, font=fonts.body, fill=1)
    draw.line((0, 16, width - 1, 16), fill=1)
    warning = ""

    # Rows 2 and 3: the facts.
    if snapshot.menu_items:
        _menu(draw, snapshot, size, fonts)
        return image
    if phase is Phase.CONNECTED:
        compass_size = 22
        text_width = width - compass_size - 4
        facts = [snapshot.charger.tag] if snapshot.charging else []
        if snapshot.battery:
            facts.append(f"Batt {snapshot.battery}")
        if snapshot.stance:
            facts.append(snapshot.stance)
        if not snapshot.led_on:
            facts.append("LED off")
        draw.text((0, 19), _fit(draw, facts, fonts.body, text_width), font=fonts.body, fill=1)
        mode = "AIM" if snapshot.aiming else "Drive"
        draw.text((0, 31), f"{mode} {snapshot.heading:03d}°", font=fonts.body, fill=1)
        _speed_bar(draw, (0, 44, 70, 50), snapshot.speed)
        draw.text((74, 41), f"{snapshot.speed:3d}", font=fonts.body, fill=1)
        _compass(draw, (width - 1 - compass_size, 19), compass_size, snapshot.heading, snapshot.aiming)
        if snapshot.charging:
            warning = snapshot.charger.warning
    elif phase is Phase.SCANNING:
        draw.text((0, 19), f"Attempt {snapshot.attempt}", font=fonts.body, fill=1)
        draw.text((0, 31), ip or "no network", font=fonts.body, fill=1)
    elif phase is Phase.SLEEPING:
        draw.text((0, 19), f"{snapshot.droid or 'Droid'} asleep", font=fonts.body, fill=1)
        draw.text((0, 31), "Looking for the next one", font=fonts.body, fill=1)
    elif phase is Phase.STOPPED:
        draw.text((0, 19), "Controller is not running", font=fonts.body, fill=1)

    # Row 4: a standing warning, else the latest event, which fades out after a few seconds.
    if warning:
        _inverted_text(draw, (0, height - 12, width - 1, height - 1), warning, fonts.body)
    elif snapshot.message and now - snapshot.message_at < MESSAGE_LIFETIME:
        draw.text((0, height - 12), snapshot.message[:24], font=fonts.body, fill=1)
    return image


MENU_ROWS = 3
MENU_ROW_HEIGHT = 12


def _menu(draw: ImageDraw.ImageDraw, snapshot: StatusSnapshot, size: tuple[int, int], fonts: Fonts) -> None:
    """A cursor list of droids; scrolls so the selected row is always visible."""
    width, height = size
    items, index = snapshot.menu_items, snapshot.menu_index
    first = max(0, min(index - MENU_ROWS + 1, len(items) - MENU_ROWS))
    for row, item in enumerate(items[first:first + MENU_ROWS]):
        y = 18 + row * MENU_ROW_HEIGHT
        chosen = first + row == index
        if chosen:
            draw.rectangle((0, y - 1, width - 1, y + MENU_ROW_HEIGHT - 2), fill=1)
        draw.text((2, y), item[:22], font=fonts.body, fill=0 if chosen else 1)
    hint = "scanning..." if snapshot.menu_scanning else "A select   B cancel"
    draw.text((0, height - 11), hint, font=fonts.body, fill=1)


def animated(snapshot: StatusSnapshot, now: float) -> bool:
    """True while the frame depends on the clock (searching dots, a message still fading)."""
    if snapshot.phase is Phase.SCANNING:
        return True
    return bool(snapshot.message) and now - snapshot.message_at < MESSAGE_LIFETIME + 1.0


def _inverted_text(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, font: Any) -> None:
    draw.rectangle(box, fill=1)
    draw.text((box[0] + 1, box[1]), text, font=font, fill=0)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: Any) -> int:
    return int(draw.textlength(text, font=font))


def _fit(draw: ImageDraw.ImageDraw, parts: list[str], font: Any, max_width: int) -> str:
    """Join parts with two spaces, dropping trailing parts until the text fits."""
    while parts:
        text = "  ".join(parts)
        if _text_width(draw, text, font) <= max_width:
            return text
        parts = parts[:-1]
    return ""


def _speed_bar(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], speed: int) -> None:
    left, top, right, bottom = box
    draw.rectangle(box, outline=1, fill=0)
    fill = round((right - left - 2) * min(max(speed, 0), 255) / 255)
    if fill > 0:
        draw.rectangle((left + 1, top + 1, left + 1 + fill, bottom - 1), fill=1)


def _compass(draw: ImageDraw.ImageDraw, origin: tuple[int, int], size: int, heading: int, aiming: bool) -> None:
    """A circle with a needle; heading 0 points up, 90 right, like the left stick."""
    left, top = origin
    radius = size // 2
    cx, cy = left + radius, top + radius
    draw.ellipse((left, top, left + size, top + size), outline=1)
    angle = math.radians(heading)
    tip = (cx + (radius - 2) * math.sin(angle), cy - (radius - 2) * math.cos(angle))
    draw.line((cx, cy, tip[0], tip[1]), fill=1, width=2 if aiming else 1)
    draw.point((cx, top - 1), fill=1)  # tick at "forward"


def local_ip() -> str:
    """The address of the interface that would route to the LAN, without sending anything."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return str(sock.getsockname()[0])
    except OSError:
        return ""


# Xbox 360 LED ring patterns, from the xpad kernel driver.
XPAD_OFF = 0
XPAD_ROTATING = 10
XPAD_PLAYER_1 = 6
XPAD_SLOW_BLINK = 12
XPAD_LED_GLOB = "/sys/class/leds/xpad*/brightness"


def led_pattern(snapshot: StatusSnapshot) -> int:
    """Rotating while looking for a droid, quadrant 1 solid while connected, slow blink asleep."""
    if snapshot.phase is Phase.CONNECTED:
        return XPAD_PLAYER_1
    if snapshot.phase in (Phase.SCANNING, Phase.STARTING):
        return XPAD_ROTATING
    if snapshot.phase is Phase.SLEEPING:
        return XPAD_SLOW_BLINK
    return XPAD_OFF


class XpadLed:
    """Mirrors the status onto the Xbox 360 controller's ring of light via sysfs."""

    def __init__(self, status: Status) -> None:
        self._status = status
        self._last: tuple[bool, int] | None = None
        self._warned = False

    def start(self) -> None:
        self._status.add_listener(self._apply)

    def stop(self) -> None:
        self._apply(self._status.snapshot())

    def _apply(self, snapshot: StatusSnapshot) -> None:
        key = (snapshot.gamepad, led_pattern(snapshot))
        if key == self._last:
            return
        self._last = key
        if not snapshot.gamepad:
            return
        pattern = key[1]
        paths = glob.glob(XPAD_LED_GLOB)
        for path in paths:
            try:
                with open(path, "w") as handle:
                    handle.write(str(pattern))
            except OSError as exc:
                if not self._warned:
                    log.warning("Cannot set the gamepad LED (%s); is the udev rule installed?", exc)
                    self._warned = True
                return
        log.debug("Gamepad LED pattern %d on %d device(s)", pattern, len(paths))
