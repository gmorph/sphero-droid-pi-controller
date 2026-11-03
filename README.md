# Sphero droid gamepad controller

Drive the Sphero Star Wars droids (R2-D2, R2-Q5, BB-8, BB-9E) from a wired Xbox 360
gamepad, with no phone and no Sphero app. A Raspberry Pi Zero W talks to the droid
over Bluetooth LE, reads the gamepad over USB, and runs as a systemd service that
starts at boot. An optional 128x64 OLED shows what the controller is doing.

Sphero withdrew the Star Wars app from the stores, which left these droids with
nothing to drive them. This project replaces the app for driving, aiming, R2's
dome, stance and animations, and the droid's LED. It uses the
[spherov2](https://github.com/artificial-intelligence-class/spherov2.py) library
for the Bluetooth protocol, with two local fixes for bugs that bite on a Pi Zero.

```
pi/          flash.sh writes and pre-configures the SD card from a Linux PC
controller/  the Python service, its config, systemd unit, udev rule and deploy script
```

## What you need

- A Raspberry Pi with Bluetooth LE and Wi-Fi. Tested on a Pi Zero W v1.1; any
  later Pi should work and will have CPU to spare.
- A micro SD card, 8 GB or larger.
- A micro USB OTG adapter for the gamepad, and a 5 V supply for the Pi.
- A wired Xbox 360 controller. The Linux `xpad` driver supports it out of the box.
  Other gamepads with two sticks and an A button are accepted but their LED ring
  and button names are untested.
- Optional: a 0.96" SSD1306 or 1.3" SH1106 128x64 I2C OLED module and four
  female-to-female jumper wires.
- A Linux PC with bash, ssh, rsync, lsblk, xz and openssl, on the same Wi-Fi
  network the Pi will join.
- One or more droids, charged. Droid names look like `D2-F5CA`, `Q5-B8D7`,
  `BB-E13C` or `GB-1A2B`; the last four characters come from the Bluetooth address.

## 1. Prepare settings on the PC

The flash script reads everything personal from `~/.config/sphero-droid/`, so
nothing secret lives in this repository.

```sh
mkdir -p ~/.config/sphero-droid && chmod 700 ~/.config/sphero-droid
cp pi/flash.conf.example ~/.config/sphero-droid/flash.conf
printf '%s' 'your-wifi-password' > ~/.config/sphero-droid/wifi.psk
chmod 600 ~/.config/sphero-droid/wifi.psk
```

Edit `flash.conf`: set `WIFI_SSID` and `WIFI_COUNTRY` (required). The other
settings default to sensible values: hostname `droid`, Pi user named after your
PC user, your PC's timezone, and every `~/.ssh/*.pub` key installed on the Pi.

Optionally write a console password for the Pi user to
`~/.config/sphero-droid/pi-password`. Without it the console password is
`sphero`. SSH always uses your keys.

Download Raspberry Pi OS Lite (32-bit) into `~/.cache/sphero-droid/`. The script
pins one release and checks its SHA-256 before writing:

```sh
mkdir -p ~/.cache/sphero-droid
cd ~/.cache/sphero-droid
curl -LO https://downloads.raspberrypi.com/raspios_lite_armhf/images/raspios_lite_armhf-2026-10-06/2026-10-06-raspios-trixie-armhf-lite.img.xz
```

To use a newer image, set `IMAGE_NAME` and `IMAGE_SHA256` in `flash.conf` to the
values published next to the download. The image must be a cloud-init enabled
release (Trixie or later), because the script configures the card through
cloud-init files on the boot partition.

## 2. Flash the SD card

The script refuses to write unless the target passes seven checks, including that
it was not present before you inserted the card. So record the baseline first:

```sh
pi/flash.sh baseline            # with the card NOT inserted
# insert the card, then find it:
lsblk -d
pi/flash.sh check /dev/sdX      # prints the device summary and every check
sudo pi/flash.sh write /dev/sdX # checks again, then writes and configures
```

The write takes a few minutes. The script then mounts the boot partition and
writes two cloud-init files: `user-data` (hostname, user, SSH keys, packages,
Bluetooth enabled, I2C enabled, Wi-Fi power saving off) and `network-config`
(the Wi-Fi network). It finishes by powering off the card reader so the card can
be pulled.

## 3. First boot

Put the card in the Pi and power it. The first boot takes three to four minutes
while cloud-init installs packages, and the Pi reboots once. Then:

```sh
ssh droid.local            # or ssh USER@HOSTNAME.local if you changed them
```

If the name does not resolve, give it another minute, then check your PC is on
the same Wi-Fi as the Pi. Guest networks usually isolate wireless clients from
the wired LAN, so a PC on Ethernet may not reach the Pi at all.

On the Pi, confirm the radios and bus are up:

```sh
bluetoothctl show | head -3          # Powered: yes
/usr/sbin/i2cdetect -y 1             # shows 3c if the OLED is wired (see section 6)
```

## 4. Deploy the controller

From the PC:

```sh
controller/deploy.sh                 # to USER@droid.local, USER = your PC user
controller/deploy.sh pi@mypi.local   # another user or host
```

Deploy copies `controller/` to `~/droid-controller` on the Pi, creates a venv at
`~/venv` that can see the apt-installed `bleak` and `evdev`, installs
`spherov2==0.12.1` and the controller into it, installs `/etc/droid-controller.toml`
if there is none, renders and installs the systemd unit and the udev rule for the
gamepad LED, enables the service, restarts it, and prints the last 30 log lines.

Day to day:

```sh
controller/deploy.sh logs            # follow the journal
controller/deploy.sh status
ssh droid.local sudo systemctl stop droid-controller    # disconnect; the droid then sleeps
ssh droid.local sudo systemctl start droid-controller
```

## 5. Drive

1. Plug the gamepad into the Pi through the OTG adapter. The ring of light rotates
   while the controller searches for a droid.
2. Wake a droid: tap it, or lift it off its charger. A sleeping droid does not
   advertise, so the controller cannot see it until it is awake.
3. The service connects to the first droid it finds. The ring of light settles on
   quadrant 1, and the screen, if fitted, shows the droid's name and battery.

| Control | Action |
| --- | --- |
| Left stick | Drive. Direction is the heading, deflection is speed |
| LT held | Aim mode: left stick turns the droid in place with its tail light on. Release to set that as straight ahead |
| Right stick X | R2 only: dome position |
| LB / RB | R2 only: bipod / tripod. R2 only rolls in tripod; moving the stick in bipod drops it to tripod first |
| A, B, X, Y, D-pad | R2 only: animations and sounds, chosen in the config |
| Start | Main LED on or off |
| Back | Droid menu: this droid, every droid in range, and "Sleep this droid". D-pad or stick moves, A selects, B cancels |
| Guide | Stop and reset aim |

Aiming: hold LT, turn the droid with the left stick until its tail light faces
you, release LT. Forward on the stick now drives away from you. Do this once after
each connect, and again if the droid drifts.

If the droid is on its USB cable (R2s) or its charging cradle (BB units) it
connects and lights up but ignores driving and stance commands. The screen shows
a standing warning and the log says so. Take it off the charger.

Full control details, tuning knobs and the local development loop are in
[controller/README.md](controller/README.md).

## 6. Status screen (optional)

Wire the OLED to the Pi's 40-pin header. Pins are numbered across the header
first: pin 1 and pin 2 are the pair at the end nearest the SD card slot, pin 1 on
the inside edge.

| OLED pin | Pi pin | Signal |
| --- | --- | --- |
| VCC | 1 | 3V3 |
| GND | 6 | Ground |
| SDA | 3 | GPIO 2, I2C data |
| SCL | 5 | GPIO 3, I2C clock |

Check with `/usr/sbin/i2cdetect -y 1` on the Pi: the module appears as `3c`. If
nothing appears, a wire is one row off. The service logs a warning and runs
without a screen if none answers.

The screen shows: the droid name and whether a gamepad is present; battery, stance
and LED state; heading with a compass and the current speed; the last action for
a few seconds; and the charging warning while the droid is on its charger. While
searching it shows the attempt number and the Pi's IP address, which is handy for
ssh. For a 1.3" SH1106 module set `driver = "sh1106"` under `[display]` in the
config.

## 7. Configuration

Edit `/etc/droid-controller.toml` on the Pi and restart the service. Every key is
documented with its default in
[controller/droid-controller.example.toml](controller/droid-controller.example.toml).
The ones most people touch:

- `[drive] max_speed` and `speed_exponent` for how fast and how gentle the stick is.
- `[droid] names` to decide which droid is taken at startup when several are awake.
- `[r2]` to map the face buttons and D-pad to animations and sounds.
- `[display]` for the OLED driver and address.
- `[logging] level = "DEBUG"` to see every drive command in the log.

## 8. Troubleshooting

Read the log first: `controller/deploy.sh logs`, or on the Pi
`journalctl -u droid-controller -f`.

- **Droid lights up but ignores everything.** It is on its charger, or Bluetooth
  on the Pi is still holding an old connection to it. The controller disconnects
  held droids before every scan and warns about chargers, so check the log for
  which it was.
- **Droid not found.** It is asleep. Tap it or lift it off the charger. A droid
  that is connected to something else does not advertise either.
- **Link drops every minute or so.** On a Pi Zero this is CPU starvation. The
  controller turns off spherov2's sensor streaming and collision reports on
  connect, which cured it; if it comes back, look for a traceback flood in the log.
- **R2 drops from bipod to tripod on its own.** Any drive or stop command does
  that, so the controller sends none while in bipod. Older firmware may still do
  it; press LB again.
- **LB does nothing and the leg does not move.** On one R2-D2 the leg motor had
  died: commands are accepted, the leg position never changes, no sound or
  vibration. That is a hardware repair.
- **BB-8 drops the link seconds after connecting.** Its battery is flat. The
  BB-8's power record reports LOW or CRITICAL and the screen shows it. BB-8 and
  BB-9E charge by induction on their cradle; the cable powers the cradle, not the
  droid, so the droid must sit properly on it.
- **Gamepad LED ring stays dark.** The udev rule did not apply. Run
  `sudo udevadm trigger --action=add --subsystem-match=leds` on the Pi, or unplug
  and replug the pad.
- **A change to `/boot/firmware/config.txt` vanished.** The Pi was powered off
  before the FAT partition synced. Run `sync` or shut down cleanly.
- **`i2cdetect: command not found` over ssh.** It lives in `/usr/sbin`, which is
  not on the non-interactive PATH.

## What the droids can and cannot do

- R2-D2 and R2-Q5 have a speaker, a dome motor, the leg mechanism and an IMU.
  Sounds and animations play on the droid itself.
- BB-8 and BB-9E have no speaker. Every sound the app made came from the phone.
  They have the body LED and the dome LEDs, and the same IMU-based driving.
- None of them have a camera, microphone or distance sensor. The firmware reports
  attitude, a dead-reckoned position, velocity, battery state and collisions.
- Batteries are replaceable on the R2s with some disassembly (a 3.7 V 1400 mAh
  pack on a proprietary connector). BB-8's shell is glued.

## Development

```sh
cd controller
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e . pytest evdev
.venv/bin/python -m pytest
.venv/bin/python -m droid_controller --dry-run --fake-gamepad --log-level DEBUG
```

The tests need no hardware. Screen rendering is tested through the pure `render`
function with Pillow; the Pi needs `python3-luma.oled` as well, which the flash
script installs. See [controller/README.md](controller/README.md) for the module
layout and the dry-run modes.

## Licence

Copyright (C) 2026 Grant Morphett. Licensed under the GNU General Public License
version 3 or later; see [LICENSE](LICENSE). You may run, study, change and share
it, and anything you distribute that includes it must be shared under the same
terms with its source. The spherov2 library it depends on is MIT licensed.
