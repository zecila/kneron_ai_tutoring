#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${PROJECT_DIR}/.env"
UNIT_TEMPLATE="${PROJECT_DIR}/deploy/kneron-livetalking.service.in"
UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
UNIT_FILE="${UNIT_DIR}/kneron-livetalking.service"

env_value() {
  sed -n "s/^${1}=//p" "${ENV_FILE}" | tail -n 1
}

livetalking_path="${LIVETALKING_PATH:-$(env_value LIVETALKING_PATH)}"
if [[ "${livetalking_path:-../LiveTalking}" = /* ]]; then
  LIVETALKING_DIR="${livetalking_path}"
else
  LIVETALKING_DIR="$(cd "${PROJECT_DIR}/${livetalking_path:-../LiveTalking}" && pwd)"
fi
PYTHON="${LIVETALKING_DIR}/.venv/bin/python"

if [[ ! -x "${PYTHON}" || ! -f "${LIVETALKING_DIR}/app.py" ]]; then
  printf 'LiveTalking or its Python environment is missing at %s\n' "${LIVETALKING_DIR}" >&2
  exit 1
fi
if "${PYTHON}" -c 'import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(("127.0.0.1", 8010)) == 0 else 1)' >/dev/null 2>&1; then
  printf 'A LiveTalking process is already using port 8010. Stop it before installing the managed service.\n' >&2
  exit 1
fi

mkdir -p "${UNIT_DIR}"
sed \
  -e "s|@PROJECT_DIR@|${PROJECT_DIR}|g" \
  -e "s|@LIVETALKING_DIR@|${LIVETALKING_DIR}|g" \
  -e "s|@PYTHON@|${PYTHON}|g" \
  "${UNIT_TEMPLATE}" > "${UNIT_FILE}"

systemctl --user daemon-reload
systemctl --user enable --now kneron-livetalking.service
if command -v loginctl >/dev/null 2>&1; then
  loginctl enable-linger "${USER}" >/dev/null 2>&1 || \
    printf 'Warning: user lingering could not be enabled; LiveTalking will start after login rather than at boot.\n' >&2
fi
printf 'Installed and started %s\n' "${UNIT_FILE}"
printf 'Follow startup with: journalctl --user -u kneron-livetalking.service -f\n'
