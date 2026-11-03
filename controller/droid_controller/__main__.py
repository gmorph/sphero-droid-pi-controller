"""Command line entry point: argument parsing and wiring."""

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, ConfigError, load_config

log = logging.getLogger("droid_controller.main")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="droid-controller", description="Drive a Sphero droid with a gamepad.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="TOML config file")
    parser.add_argument("--dry-run", action="store_true", help="use a fake droid that logs commands")
    parser.add_argument("--fake-gamepad", action="store_true", help="replay a scripted gamepad sequence")
    parser.add_argument("--log-level", help="override the configured log level")
    return parser.parse_args(argv)


SHUTDOWN_DEADLINE = 12.0  # seconds; a dead BLE link makes the clean path block on 10 s command timeouts


def install_signal_handlers(stop: threading.Event, wake: threading.Event) -> None:
    def force_exit() -> None:
        log.warning("Clean shutdown took longer than %.0f s; exiting now", SHUTDOWN_DEADLINE)
        logging.shutdown()
        os._exit(0)

    def handler(signum: int, _frame: object) -> None:
        if stop.is_set():
            return
        log.info("Received %s, shutting down", signal.Signals(signum).name)
        stop.set()
        wake.set()
        watchdog = threading.Timer(SHUTDOWN_DEADLINE, force_exit)
        watchdog.daemon = True
        watchdog.start()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Libraries (bleak, spherov2) stay at WARNING; the configured level applies to our own package only.
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("droid_controller").setLevel((args.log_level or config.logging.level).upper())
    # Imported here so a bad config is reported before spherov2 and evdev load.
    from .app import Controller
    from .display import OledDisplay, XpadLed, open_screen
    from .droid import FakeConnector, SpheroConnector
    from .gamepad import DEMO_SCRIPT, EvdevGamepad, GamepadSource, ScriptedGamepad
    from .status import Status

    stop = threading.Event()
    wake = threading.Event()
    install_signal_handlers(stop, wake)
    status = Status()
    gamepad: GamepadSource
    if args.fake_gamepad:
        gamepad = ScriptedGamepad(DEMO_SCRIPT, stop, wake)
        status.update(gamepad=True)
    else:
        gamepad = EvdevGamepad(config.gamepad, wake, on_connection=lambda connected: status.update(gamepad=connected))
    connector = FakeConnector() if args.dry_run else SpheroConnector(config.droid)
    log.info("droid-controller starting (config %s)", args.config if args.config.is_file() else "defaults")
    feedback = []
    if config.display.enabled:
        screen = open_screen(config.display)
        if screen is not None:
            feedback.append(OledDisplay(config.display, status, screen))
    if config.gamepad.led:
        feedback.append(XpadLed(status))
    for item in feedback:
        item.start()
    gamepad.start()
    try:
        Controller(config, gamepad, connector, stop, wake, status).run()
    finally:
        gamepad.stop()
        for item in feedback:
            item.stop()
    log.info("droid-controller exiting")
    # spherov2 and bleak leave non-daemon threads behind after a failed disconnect, which would
    # keep the process alive after main returns; the loop is done, so end the process outright.
    logging.shutdown()
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
