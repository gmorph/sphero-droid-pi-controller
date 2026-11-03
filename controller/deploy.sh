#!/usr/bin/env bash
# Deploy droid-controller to the Raspberry Pi and manage the service.
#
# Usage:
#   deploy.sh [[USER@]HOST]          rsync, install into the venv, install the unit, restart, show logs
#   deploy.sh logs [[USER@]HOST]     follow the service journal
#   deploy.sh status [[USER@]HOST]   show the service status
#
# HOST defaults to droid.local and USER to your local user name (the flash script
# creates the Pi user with that name). Set DROID_HOST to change the default.
set -euo pipefail

REMOTE_DIR="droid-controller"
SERVICE="droid-controller"
SPHEROV2_VERSION="0.12.1"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() { echo "usage: $0 [deploy|logs|status] [[USER@]HOST]" >&2; exit 1; }

cmd="deploy"
case "${1:-}" in
    logs|status) cmd="$1"; shift ;;
    deploy) shift ;;
    -h|--help) usage ;;
esac
[[ $# -le 1 ]] || usage
TARGET="${1:-${DROID_HOST:-droid.local}}"
[[ "$TARGET" == *@* ]] || TARGET="$(id -un)@${TARGET}"
PI_USER="${TARGET%%@*}"
HOST="${TARGET#*@}"
VENV="/home/${PI_USER}/venv"

case "$cmd" in
    logs)
        exec ssh -t "$TARGET" "journalctl -u ${SERVICE} -f -o short-iso"
        ;;
    status)
        exec ssh -t "$TARGET" "systemctl status ${SERVICE} --no-pager"
        ;;
esac

echo "== Syncing ${SRC_DIR} to ${TARGET}:~/${REMOTE_DIR}"
rsync -az --delete \
    --exclude tests --exclude __pycache__ --exclude .pytest_cache --exclude '*.egg-info' \
    "${SRC_DIR}/" "${TARGET}:${REMOTE_DIR}/"

echo "== Installing on ${HOST}"
ssh "$TARGET" bash -s <<REMOTE
set -euo pipefail
if [[ ! -x "${VENV}/bin/python" ]]; then
    echo "  creating venv ${VENV}"
    python3 -m venv --system-site-packages "${VENV}"
fi
# The editable install is slow on a Pi Zero; redo it only when packaging metadata changes.
STAMP="${VENV}/.droid-controller.pyproject.sha256"
WANT=\$(sha256sum "\$HOME/${REMOTE_DIR}/pyproject.toml" | cut -d" " -f1)
if [[ ! -f "\$STAMP" || "\$(cat "\$STAMP")" != "\$WANT" ]]; then
    echo "  installing spherov2 and the controller package into the venv"
    "${VENV}/bin/pip" install --quiet "spherov2==${SPHEROV2_VERSION}"
    "${VENV}/bin/pip" install --quiet -e "\$HOME/${REMOTE_DIR}"
    echo "\$WANT" > "\$STAMP"
fi
if [[ ! -f /etc/${SERVICE}.toml ]]; then
    echo "  installing default /etc/${SERVICE}.toml"
    sudo install -m 0644 "\$HOME/${REMOTE_DIR}/${SERVICE}.example.toml" "/etc/${SERVICE}.toml"
fi
# The unit is a template: fill in the user it runs as and the venv it runs from.
sed "s|@PI_USER@|${PI_USER}|g; s|@VENV@|${VENV}|g" "\$HOME/${REMOTE_DIR}/${SERVICE}.service" \
    | sudo install -m 0644 /dev/stdin "/etc/systemd/system/${SERVICE}.service"
# Let the service drive the Xbox 360 LED ring, which sysfs exposes as root-only.
if ! cmp -s "\$HOME/${REMOTE_DIR}/99-${SERVICE}.rules" /etc/udev/rules.d/99-${SERVICE}.rules; then
    echo "  installing udev rule for the gamepad LED"
    sudo install -m 0644 "\$HOME/${REMOTE_DIR}/99-${SERVICE}.rules" "/etc/udev/rules.d/99-${SERVICE}.rules"
    sudo udevadm control --reload && sudo udevadm trigger --action=add --subsystem-match=leds
fi
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE}" >/dev/null 2>&1
sudo systemctl restart "${SERVICE}"
sleep 2
journalctl -u "${SERVICE}" -n 30 --no-pager -o short-iso
REMOTE
