# droid-controller

Drives Sphero Star Wars droids (R2-D2, R2-Q5, BB-8, BB-9E) from a wired Xbox 360
gamepad. Runs as a systemd service on a Raspberry Pi Zero W. The top-level
[README](../README.md) covers flashing the Pi and the first-time setup; this file
covers the service itself.

## Layout

| File | Role |
| --- | --- |
| `droid_controller/__main__.py` | Entry point: loads config, builds the gamepad, status feedback and controller, handles signals |
| `droid_controller/app.py` | `Controller` (scan, connect, retry, switch, sleep) and `Session` (one connected droid: drive loop, buttons, menu, power checks) |
| `droid_controller/droid.py` | Bluetooth side: scanning, connecting, power reads, the `Droid` model, stale-connection cleanup, fake droid for dry runs |
| `droid_controller/gamepad.py` | evdev gamepad reader with hot-plug, plus a scripted fake gamepad |
| `droid_controller/mapping.py` | Pure stick-to-drive maths: dead zone, speed curve, heading, aim turning |
| `droid_controller/menu.py` | The droid chooser opened with Back |
| `droid_controller/status.py` | Thread-safe status snapshot the screen and LED ring subscribe to |
| `droid_controller/display.py` | OLED rendering (pure `render` function plus the luma.oled thread) and the Xbox LED ring |
| `droid_controller/config.py` | TOML config with defaults and validation |
| `droid_controller/spherov2_fixes.py` | Runtime patch for spherov2's packet collector, which loses sync after one bad packet |
| `droid-controller.example.toml` | Every setting, documented, with its default |
| `droid-controller.service` | systemd unit template; `deploy.sh` fills in the user and venv |
| `99-droid-controller.rules` | udev rule that lets the service set the gamepad LED ring |
| `deploy.sh` | rsync to the Pi, install, restart, logs |
| `tests/` | pytest suite; runs without hardware |

## Run locally

Create a venv with `spherov2==0.12.1`, `evdev` and `pytest` (on the Pi, evdev and
bleak come from apt and the venv is created with `--system-site-packages`), then:

```sh
pip install -e .
pytest
python -m droid_controller --dry-run --fake-gamepad --log-level DEBUG   # no hardware at all
python -m droid_controller --dry-run --log-level DEBUG                  # real gamepad, fake droid
python -m droid_controller --config ./droid-controller.example.toml     # real droid
```

`--dry-run` replaces the droid with a fake R2-D2 that logs every command.
`--fake-gamepad` replays a scripted sequence covering every control and exits.
Drive commands are logged at DEBUG only.

## Deploy to the Pi

```sh
./deploy.sh                      # to droid.local as your local user name
./deploy.sh pi@other.local       # another user or host (or set DROID_HOST)
./deploy.sh logs [[USER@]HOST]   # follow the journal
./deploy.sh status [[USER@]HOST]
```

Deploy rsyncs this directory to `~/droid-controller` on the Pi, installs it into
`~/venv` there, installs `/etc/droid-controller.toml` if absent, renders and
installs the `droid-controller` unit and the udev rule, restarts the service and
prints the last 30 log lines.

## Controls

| Control | Action |
| --- | --- |
| Left stick | Drive: direction is the heading (up = 0, right = 90), deflection is speed |
| LT (hold) | Aim mode: left stick X turns the droid in place, back LED on; release resets heading 0 |
| Right stick X | R2 only: dome position |
| LB / RB | R2 only: bipod / tripod stance |
| A / B / X / Y | R2 only: animations (configurable) |
| D-pad | R2 only: animations or sounds (configurable) |
| Start | Toggle main LED on/off |
| Back (select) | Open the droid menu on the screen: the current droid, every droid in range, and "Sleep this droid". D-pad or left stick moves, A selects, B or Back cancels |
| Guide | Stop and reset aim |

## Status feedback

The Xbox 360 ring of light shows the state: rotating while searching for a droid,
quadrant 1 solid while connected, slow blink while the droid is being put to sleep.

An optional 128x64 I2C OLED (SSD1306 0.96" or SH1106 1.3") on the Pi's I2C pins
shows the droid name, battery, gamepad presence, heading and speed with a compass,
stance and the last action. Wiring: VCC to pin 1 (3V3), GND to pin 6, SDA to pin 3
(GPIO 2), SCL to pin 5 (GPIO 3). See `[display]` in the example config. Check the
screen is seen with `i2cdetect -y 1` (address 3c).

Safety: the droid is stopped when the gamepad disconnects, when no gamepad event
arrives for `idle_timeout` seconds, and on SIGTERM/SIGINT. The droid connection
is retried forever with backoff.

## Tuning

Choosing another droid from the menu puts the current one to sleep and connects
to the chosen one by name. Without a screen the menu still works blind: the
first entry is the current droid, then the droids found, then sleep.

Edit `/etc/droid-controller.toml` (every key is commented in
`droid-controller.example.toml`; a missing file means defaults) and restart the
service. The usual knobs are `[drive] max_speed` and `speed_exponent` for feel,
`[gamepad] dead_zone` for a worn stick, `[droid] name_prefixes` to pin a droid,
`[r2]` to choose animations and sounds, and `[logging] level = "DEBUG"` to see
drive commands.
