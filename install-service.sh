#!/bin/bash
# Installs files only. Starting/enabling the service is a separate command.
set -euo pipefail
if [ "$#" -ne 1 ] || [[ ! "$1" =~ ^GPU-[A-Za-z0-9-]+$ ]]; then
    echo 'Usage: sudo bash install-service.sh GPU-<exact-UUID>' >&2
    exit 2
fi
if [ "$EUID" -ne 0 ]; then
    echo 'Run this installer with sudo.' >&2
    exit 2
fi
uuid="$1"
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if systemctl is-active --quiet "gpu-fan-curve@$uuid.service"; then
    echo 'Service is running. Stop it before replacing its files:' >&2
    echo "sudo systemctl stop gpu-fan-curve@$uuid.service" >&2
    exit 1
fi
# Validate before installing; loading this module never initializes NVML.
/usr/bin/python3 - "$source_dir" <<'PY'
import importlib.util
from pathlib import Path
import sys
root=Path(sys.argv[1])
spec=importlib.util.spec_from_file_location('fan_control',root/'control.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.load_config(root/'curve.json')
PY
install -d -o root -g root -m 755 /opt/gpu-fan-control "/opt/gpu-fan-control/$uuid" /etc/gpu-fan-control
install -o root -g root -m 644 "$source_dir/control.py" "/opt/gpu-fan-control/$uuid/control.py"
install -o root -g root -m 644 "$source_dir/curve.json" "/etc/gpu-fan-control/$uuid.json"
install -o root -g root -m 644 "$source_dir/gpu-fan-curve@.service" /etc/systemd/system/gpu-fan-curve@.service
systemctl daemon-reload
echo 'Installed. Not started or enabled yet.'
echo "Start and enable: sudo systemctl enable --now gpu-fan-curve@$uuid.service"
