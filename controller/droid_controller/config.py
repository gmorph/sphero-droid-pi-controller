"""Configuration dataclasses with defaults, loaded from a TOML file."""

import dataclasses
import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("/etc/droid-controller.toml")

ACTION_KINDS = ("animation", "sound")
R2_ACTION_BUTTONS = ("a", "b", "x", "y", "dpad_up", "dpad_down", "dpad_left", "dpad_right")
DISPLAY_DRIVERS = ("ssd1306", "sh1106")


class ConfigError(ValueError):
    """The configuration file is malformed or contains invalid values."""


@dataclass(frozen=True)
class GamepadConfig:
    dead_zone: float = 0.15
    preferred_names: tuple[str, ...] = ("Xbox", "X-Box")
    rescan_interval: float = 1.0
    idle_timeout: float = 60.0
    led: bool = True


@dataclass(frozen=True)
class DriveConfig:
    max_speed: int = 180
    speed_exponent: float = 1.5
    update_rate: float = 10.0
    heartbeat_interval: float = 5.0
    aim_trigger_threshold: float = 0.5
    aim_turn_rate: float = 90.0
    heading_offset: int = 0


@dataclass(frozen=True)
class DroidConfig:
    names: tuple[str, ...] = ()
    name_prefixes: tuple[str, ...] = ()
    scan_timeout: float = 5.0
    reconnect_backoff_min: float = 2.0
    reconnect_backoff_max: float = 30.0
    sleep_rescan_delay: float = 5.0
    main_led: tuple[int, int, int] = (0, 100, 255)


@dataclass(frozen=True)
class R2Config:
    """Button to action mapping for R2-D2 and R2-Q5, as "animation:NAME" or "sound:NAME"."""

    a: str = "animation:EMOTE_YES"
    b: str = "animation:EMOTE_NO"
    x: str = "animation:EMOTE_LAUGH"
    y: str = "animation:EMOTE_EXCITED"
    dpad_up: str = "animation:WWM_BOW"
    dpad_down: str = "animation:EMOTE_ALARM"
    dpad_left: str = "sound:R2_SCREAM"
    dpad_right: str = "sound:R2_LAUGH_1"


@dataclass(frozen=True)
class DisplayConfig:
    """An I2C OLED status screen (luma.oled). Absent hardware is tolerated with a warning."""

    enabled: bool = True
    driver: str = "ssd1306"
    i2c_port: int = 1
    i2c_address: int = 0x3C
    width: int = 128
    height: int = 64
    rotate: int = 0
    refresh_rate: float = 4.0
    contrast: int = 255


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"


@dataclass(frozen=True)
class Config:
    gamepad: GamepadConfig = field(default_factory=GamepadConfig)
    drive: DriveConfig = field(default_factory=DriveConfig)
    droid: DroidConfig = field(default_factory=DroidConfig)
    r2: R2Config = field(default_factory=R2Config)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


_SECTIONS = {
    "gamepad": GamepadConfig,
    "drive": DriveConfig,
    "droid": DroidConfig,
    "r2": R2Config,
    "display": DisplayConfig,
    "logging": LoggingConfig,
}


def load_config(path: Path | None = None) -> Config:
    """Load the TOML file at path; a missing file yields the defaults."""
    path = path or DEFAULT_CONFIG_PATH
    if not path.is_file():
        return Config()
    with path.open("rb") as handle:
        try:
            data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
    return config_from_dict(data)


def config_from_dict(data: dict[str, Any]) -> Config:
    """Build a Config from parsed TOML, rejecting unknown keys and invalid values."""
    unknown = set(data) - set(_SECTIONS)
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(sorted(unknown))}")
    sections = {}
    for name, cls in _SECTIONS.items():
        table = data.get(name, {})
        if not isinstance(table, dict):
            raise ConfigError(f"[{name}] must be a table")
        sections[name] = _build_section(cls, table, name)
    config = Config(**sections)
    _validate(config)
    return config


def _build_section(cls: type, table: dict[str, Any], section: str) -> Any:
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(table) - set(fields)
    if unknown:
        raise ConfigError(f"[{section}] unknown key(s): {', '.join(sorted(unknown))}")
    values = {}
    for key, raw in table.items():
        values[key] = _coerce(raw, fields[key].default, f"[{section}] {key}")
    return cls(**values)


def _coerce(raw: Any, default: Any, label: str) -> Any:
    """Coerce a TOML value to the type of the dataclass default, or raise ConfigError."""
    if isinstance(default, bool):
        if not isinstance(raw, bool):
            raise ConfigError(f"{label} must be a boolean")
        return raw
    if isinstance(default, int):
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise ConfigError(f"{label} must be an integer")
        return raw
    if isinstance(default, float):
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            raise ConfigError(f"{label} must be a number")
        return float(raw)
    if isinstance(default, str):
        if not isinstance(raw, str):
            raise ConfigError(f"{label} must be a string")
        return raw
    if isinstance(default, tuple):
        if not isinstance(raw, list):
            raise ConfigError(f"{label} must be an array")
        return tuple(raw)
    raise ConfigError(f"{label} has an unsupported type")


def _validate(config: Config) -> None:
    gamepad, drive, droid = config.gamepad, config.drive, config.droid
    _require(0.0 <= gamepad.dead_zone < 1.0, "[gamepad] dead_zone must be in 0.0 .. 0.99")
    _require(all(isinstance(n, str) for n in gamepad.preferred_names),
             "[gamepad] preferred_names must be an array of strings")
    _require(gamepad.rescan_interval > 0, "[gamepad] rescan_interval must be positive")
    _require(gamepad.idle_timeout >= 0, "[gamepad] idle_timeout must not be negative")
    _require(1 <= drive.max_speed <= 255, "[drive] max_speed must be in 1 .. 255")
    _require(drive.speed_exponent > 0, "[drive] speed_exponent must be positive")
    _require(drive.update_rate > 0, "[drive] update_rate must be positive")
    _require(drive.heartbeat_interval >= 0, "[drive] heartbeat_interval must not be negative")
    _require(0.0 < drive.aim_trigger_threshold <= 1.0, "[drive] aim_trigger_threshold must be in 0.0 .. 1.0")
    _require(-359 <= drive.heading_offset <= 359, "[drive] heading_offset must be in -359 .. 359")
    _require(all(isinstance(n, str) for n in droid.names), "[droid] names must be an array of strings")
    _require(drive.aim_turn_rate > 0, "[drive] aim_turn_rate must be positive")
    _require(all(isinstance(p, str) for p in droid.name_prefixes),
             "[droid] name_prefixes must be an array of strings")
    _require(droid.scan_timeout > 0, "[droid] scan_timeout must be positive")
    _require(0 < droid.reconnect_backoff_min <= droid.reconnect_backoff_max,
             "[droid] reconnect_backoff_min must be positive and not above reconnect_backoff_max")
    _require(droid.sleep_rescan_delay >= 0, "[droid] sleep_rescan_delay must not be negative")
    _require(len(droid.main_led) == 3 and all(isinstance(c, int) and 0 <= c <= 255 for c in droid.main_led),
             "[droid] main_led must be three integers in 0 .. 255")
    for button in R2_ACTION_BUTTONS:
        parse_action(getattr(config.r2, button), f"[r2] {button}")
    display = config.display
    _require(display.driver in DISPLAY_DRIVERS, f"[display] driver must be one of {', '.join(DISPLAY_DRIVERS)}")
    _require(display.i2c_port >= 0, "[display] i2c_port must not be negative")
    _require(0x08 <= display.i2c_address <= 0x77, "[display] i2c_address must be in 0x08 .. 0x77")
    _require(display.width > 0 and display.height > 0, "[display] width and height must be positive")
    _require(display.rotate in (0, 1, 2, 3), "[display] rotate must be 0, 1, 2 or 3 (quarter turns)")
    _require(display.refresh_rate > 0, "[display] refresh_rate must be positive")
    _require(0 <= display.contrast <= 255, "[display] contrast must be in 0 .. 255")
    _require(config.logging.level.upper() in logging.getLevelNamesMapping(),
             f"[logging] level {config.logging.level!r} is not a logging level")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def parse_action(spec: str, label: str = "action") -> tuple[str, str]:
    """Split "animation:NAME" or "sound:NAME" into (kind, name)."""
    kind, _, name = spec.partition(":")
    if kind not in ACTION_KINDS or not name:
        raise ConfigError(f"{label} must look like 'animation:NAME' or 'sound:NAME', got {spec!r}")
    return kind, name
