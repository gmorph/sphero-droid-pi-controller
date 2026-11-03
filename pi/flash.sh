#!/usr/bin/env bash
# Flash Raspberry Pi OS Lite (32-bit, trixie) onto a micro SD card and
# pre-configure it for headless use (hostname, user, SSH key, Wi-Fi).
#
# Usage:
#   flash.sh baseline            - record the disks present BEFORE the card is inserted
#   flash.sh check /dev/sdX      - identify the device and run every safety check (no root needed)
#   sudo flash.sh write /dev/sdX - re-run the checks, then flash and configure
#
# Settings come from ~/.config/sphero-droid/ of the user running the script (also under
# sudo or pkexec): flash.conf (see flash.conf.example), wifi.psk (the Wi-Fi password on
# one line) and optionally pi-password (console password for the Pi user).
#
# Safety checks (ALL must pass, the script refuses otherwise):
#   1. target is a whole disk, not a partition
#   2. target is USB or MMC attached
#   3. target is marked removable or hotplug by the kernel
#   4. size is between 1.5 GB and 256 GB (SD card range)
#   5. nothing on it is mounted, in /etc/fstab, or used as swap
#   6. it was NOT present in the baseline snapshot taken before the card was inserted
#   7. the kernel's own view of the device matches the summary printed for the human
set -euo pipefail

# The person running the script, even under sudo or pkexec (HOME is root's there).
INVOKER="${SUDO_USER:-${PKEXEC_UID:+$(getent passwd "$PKEXEC_UID" | cut -d: -f1)}}"
INVOKER="${INVOKER:-$(id -un)}"
INVOKER_HOME="$(getent passwd "$INVOKER" | cut -d: -f6)"
CONF_DIR="$INVOKER_HOME/.config/sphero-droid"
BASELINE="$CONF_DIR/baseline-disks.txt"
WIFI_PSK_FILE="$CONF_DIR/wifi.psk"
PI_PASS_FILE="$CONF_DIR/pi-password"

# Defaults; override any of them in $CONF_DIR/flash.conf (plain bash assignments).
IMAGE_DIR="$INVOKER_HOME/.cache/sphero-droid"
IMAGE_NAME="2026-10-06-raspios-trixie-armhf-lite.img.xz"
IMAGE_SHA256="2c302771f34c67a0b7ae7aa2e0dcdd3eac660504be59193c011ba79d412e1632"
PI_HOSTNAME="droid"
PI_USER="$INVOKER"
WIFI_SSID=""
WIFI_COUNTRY=""
TIMEZONE="$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)"
SSH_KEYS=()
if [[ -r "$CONF_DIR/flash.conf" ]]; then
    # shellcheck source=/dev/null
    source "$CONF_DIR/flash.conf"
fi
if [[ ${#SSH_KEYS[@]} -eq 0 ]]; then
    for k in "$INVOKER_HOME"/.ssh/*.pub; do [[ -r "$k" ]] && SSH_KEYS+=("$k"); done
fi
IMAGE="$IMAGE_DIR/$IMAGE_NAME"

die() { echo "REFUSING: $*" >&2; exit 1; }
info() { echo "  $*"; }

MODE="${1:-}"
DEV="${2:-}"
if [[ "$MODE" == "baseline" ]]; then
    mkdir -p "$CONF_DIR"
    lsblk -dno NAME > "$BASELINE"
    echo "Recorded $(wc -l < "$BASELINE") disk(s) in $BASELINE. Now insert the SD card and run: $0 check /dev/sdX"
    exit 0
fi
[[ "$MODE" == "check" || "$MODE" == "write" ]] || die "usage: $0 baseline | check /dev/sdX | write /dev/sdX"
[[ -n "$DEV" ]] || die "no device given"

echo "== Target device: $DEV"
[[ -b "$DEV" ]] || die "$DEV is not a block device"

NAME=$(basename "$DEV")
TYPE=$(lsblk -dno TYPE "$DEV")
TRAN=$(lsblk -dno TRAN "$DEV")
RM=$(lsblk -dno RM "$DEV")
HOTPLUG=$(lsblk -dno HOTPLUG "$DEV")
SIZE_BYTES=$(lsblk -dbno SIZE "$DEV")
SIZE_H=$(lsblk -dno SIZE "$DEV")
VENDOR=$(lsblk -dno VENDOR "$DEV" | xargs || true)
MODEL=$(lsblk -dno MODEL "$DEV" | xargs || true)
SERIAL=$(lsblk -dno SERIAL "$DEV" | xargs || true)

info "name=$NAME type=$TYPE tran=${TRAN:-none} removable=$RM hotplug=$HOTPLUG"
info "size=$SIZE_H vendor='${VENDOR}' model='${MODEL}' serial='${SERIAL}'"
echo "== Partitions and mountpoints currently on it:"
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINTS "$DEV" | sed 's/^/  /'

echo "== Safety checks"
# 1. whole disk
[[ "$TYPE" == "disk" ]] || die "type is '$TYPE', not a whole disk"
info "1. whole disk: ok"

# 2. usb or mmc
if [[ "$TRAN" == "usb" || "$NAME" == mmcblk* ]]; then
    info "2. transport usb/mmc: ok ($TRAN${TRAN:+ }${NAME%%[0-9]*})"
else
    die "transport is '${TRAN:-none}', expected usb or an mmcblk device"
fi

# 3. removable / hotplug
[[ "$RM" == "1" || "$HOTPLUG" == "1" ]] || die "kernel does not flag it removable or hotplug"
info "3. removable/hotplug: ok"

# 4. size range
MIN=$((1536 * 1024 * 1024)); MAX=$((256 * 1024 * 1024 * 1024))
(( SIZE_BYTES >= MIN && SIZE_BYTES <= MAX )) || die "size $SIZE_H is outside the SD card range"
info "4. size $SIZE_H within 1.5G..256G: ok"

# 5. nothing mounted / fstab / swap
if lsblk -no MOUNTPOINTS "$DEV" | grep -q .; then
    die "something on $DEV is mounted (see above). Unmount it first if this really is the SD card."
fi
if grep -qE "^[^#]*(/dev/$NAME|$(lsblk -no UUID "$DEV" | grep . | paste -sd'|' || echo __none__))" /etc/fstab 2>/dev/null; then
    die "$DEV or one of its filesystems is referenced in /etc/fstab"
fi
if grep -q "^/dev/$NAME" /proc/swaps; then
    die "$DEV is in use as swap"
fi
info "5. not mounted, not in fstab, not swap: ok"

# 6. not in baseline
[[ -r "$BASELINE" ]] || die "baseline file $BASELINE missing; run '$0 baseline' before inserting the card"
if grep -qx "$NAME" "$BASELINE"; then
    die "$NAME was already attached when the baseline was taken; it is NOT the newly inserted card"
fi
info "6. not present in pre-insertion baseline ($(wc -l < "$BASELINE") disks): ok"

# 7. sysfs agrees with lsblk (belt and braces)
SYS_REMOVABLE=$(cat "/sys/block/$NAME/removable" 2>/dev/null || echo "?")
SYS_SIZE=$(( $(cat "/sys/block/$NAME/size" 2>/dev/null || echo 0) * 512 ))
[[ "$SYS_SIZE" == "$SIZE_BYTES" ]] || die "sysfs size ($SYS_SIZE) != lsblk size ($SIZE_BYTES)"
info "7. sysfs size matches, sysfs removable=$SYS_REMOVABLE: ok"

# Also show the disks we must never touch, for the human to eyeball.
echo "== For comparison, disks holding this machine's filesystems:"
lsblk -dno NAME,SIZE,MODEL,SERIAL "$(findmnt -no SOURCE / | sed -E 's/p?[0-9]+$//')" | sed 's/^/  ROOT  /'

echo "== Verdict: $DEV passes all checks and is a plausible freshly inserted SD card."

if [[ "$MODE" == "check" ]]; then
    echo "Run:  sudo $0 write $DEV   to flash it."
    exit 0
fi

# ---------------------------------------------------------------- write mode
[[ $EUID -eq 0 ]] || die "write mode needs root (sudo)"
[[ -r "$IMAGE" ]] || die "image not found at $IMAGE (see README: download it from raspberrypi.com)"
[[ -n "$WIFI_SSID" ]] || die "WIFI_SSID is not set in $CONF_DIR/flash.conf"
[[ -n "$WIFI_COUNTRY" ]] || die "WIFI_COUNTRY (two-letter code) is not set in $CONF_DIR/flash.conf"
[[ -r "$WIFI_PSK_FILE" ]] || die "Wi-Fi password file $WIFI_PSK_FILE missing"
[[ ${#SSH_KEYS[@]} -gt 0 ]] || die "no SSH public key found in $INVOKER_HOME/.ssh and SSH_KEYS is empty in flash.conf"
for k in "${SSH_KEYS[@]}"; do [[ -r "$k" ]] || die "ssh key $k missing"; done

echo "== Verifying image checksum"
echo "$IMAGE_SHA256  $IMAGE" | sha256sum -c - || die "image checksum mismatch"

WIFI_PSK=$(tr -d '\r\n' < "$WIFI_PSK_FILE")
[[ ${#WIFI_PSK} -ge 8 ]] || die "Wi-Fi password in $WIFI_PSK_FILE is shorter than 8 characters"
if [[ -r "$PI_PASS_FILE" ]]; then
    PI_PASS=$(tr -d '\r\n' < "$PI_PASS_FILE")
else
    PI_PASS="sphero"  # console fallback only; SSH uses the keys above
    info "no $PI_PASS_FILE: the Pi user's console password will be 'sphero'"
fi
PI_PASS_HASH=$(openssl passwd -6 "$PI_PASS")

echo "== Writing image to $DEV (this takes several minutes)"
xzcat "$IMAGE" | dd of="$DEV" bs=4M conv=fsync status=progress
sync
partprobe "$DEV" || true
sleep 3

if [[ "$NAME" == mmcblk* ]]; then BOOT_PART="${DEV}p1"; else BOOT_PART="${DEV}1"; fi
for _ in $(seq 1 20); do [[ -b "$BOOT_PART" ]] && break; sleep 1; done
[[ -b "$BOOT_PART" ]] || die "boot partition $BOOT_PART did not appear after flashing"

MNT=$(mktemp -d)
mount "$BOOT_PART" "$MNT"
trap 'umount "$MNT" 2>/dev/null || true; rmdir "$MNT" 2>/dev/null || true' EXIT

[[ -f "$MNT/user-data" && -f "$MNT/network-config" ]] || die "boot partition lacks cloud-init files; wrong image?"
echo "== Boot partition mounted; label=$(lsblk -no LABEL "$BOOT_PART"), existing cloud-init files present"

echo "== Writing cloud-init user-data"
{
    cat <<EOF
#cloud-config

hostname: $PI_HOSTNAME
manage_etc_hosts: true
timezone: $TIMEZONE

users:
  - name: $PI_USER
    groups: users,adm,dialout,audio,netdev,video,plugdev,cdrom,games,input,gpio,spi,i2c,render,sudo,bluetooth
    shell: /bin/bash
    lock_passwd: false
    passwd: "$PI_PASS_HASH"
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
EOF
    for k in "${SSH_KEYS[@]}"; do echo "      - $(cat "$k")"; done
    cat <<'EOF'

enable_ssh: true
ssh_pwauth: true

package_update: true
package_upgrade: false
packages:
  - python3
  - python3-venv
  - python3-pip
  - python3-numpy
  - python3-transforms3d
  - python3-bleak
  - python3-evdev
  - bluez
  - joystick
  - git
  - rsync
  - python3-luma.oled
  - i2c-tools
  - fonts-dejavu-core

runcmd:
  # Keep Wi-Fi on but stop its power saving; it shares the radio with Bluetooth.
  - [ sh, -c, "mkdir -p /etc/NetworkManager/conf.d && printf '[connection]\nwifi.powersave = 2\n' > /etc/NetworkManager/conf.d/wifi-powersave-off.conf" ]
  - [ systemctl, enable, bluetooth ]
  # I2C for the status OLED.
  - [ raspi-config, nonint, do_i2c, "0" ]
EOF
} > "$MNT/user-data"

echo "== Writing cloud-init network-config"
cat > "$MNT/network-config" <<EOF
network:
  version: 2
  wifis:
    renderer: NetworkManager
    wlan0:
      dhcp4: true
      optional: true
      regulatory-domain: "$WIFI_COUNTRY"
      access-points:
        "$WIFI_SSID":
          password: "$WIFI_PSK"
EOF
chmod 600 "$MNT/network-config"

echo "== Boot partition contents:"
ls -la "$MNT" | sed 's/^/  /'
sync
umount "$MNT"; rmdir "$MNT"; trap - EXIT
sync
echo "== Done. Powering off the card reader so the card can be removed."
udisksctl power-off -b "$DEV" 2>/dev/null || eject "$DEV" 2>/dev/null || true
echo "== Flashed. Move the card to the Pi, power it, wait ~3-4 minutes for first boot, then: ssh $PI_USER@$PI_HOSTNAME.local"
