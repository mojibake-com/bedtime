#!/bin/bash
# Link Bedtime into a Klipper + Mainsail install on the printer host.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXTRAS="${HOME}/klipper/klippy/extras"
CONFIG="${HOME}/printer_data/config"

if [[ ! -d "${EXTRAS}" ]]; then
    echo "Klipper extras directory not found at ${EXTRAS}." >&2
    echo "Run this on the printer host, from a user that owns ~/klipper." >&2
    exit 1
fi

ln -sfn "${ROOT}/bedtime.py" "${EXTRAS}/bedtime.py"
echo "Linked ${EXTRAS}/bedtime.py -> ${ROOT}/bedtime.py"

if [[ -d "${CONFIG}" ]]; then
    if [[ -e "${CONFIG}/bedtime.cfg" ]]; then
        echo "Left existing ${CONFIG}/bedtime.cfg in place."
    else
        cp "${ROOT}/bedtime.cfg" "${CONFIG}/bedtime.cfg"
        echo "Copied ${CONFIG}/bedtime.cfg"
    fi
    echo "Add this line to printer.cfg if it is not there yet:"
    echo "  [include bedtime.cfg]"
else
    echo "No ${CONFIG} directory. Copy bedtime.cfg into your config folder yourself."
fi

echo "Restart Klipper when the include is in place:"
echo "  sudo systemctl restart klipper"
