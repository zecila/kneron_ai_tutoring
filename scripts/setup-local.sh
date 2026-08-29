#!/usr/bin/env bash

set -u
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${PROJECT_DIR}/.env"
errors=0

ok() {
  printf '[ok] %s\n' "$1"
}

problem() {
  printf '[missing] %s\n' "$1" >&2
  errors=$((errors + 1))
}

if ! command -v docker >/dev/null 2>&1; then
  problem "Docker is not installed"
elif ! docker compose version >/dev/null 2>&1; then
  problem "Docker Compose is not available through 'docker compose'"
else
  ok "Docker Compose is installed"
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
  problem "nvidia-smi is not available; LiveTalking requires an NVIDIA GPU"
elif ! nvidia-smi >/dev/null 2>&1; then
  problem "the NVIDIA GPU or driver is not available"
else
  ok "NVIDIA GPU is available"
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${PROJECT_DIR}/.env.example" "${ENV_FILE}"
  printf '[created] %s\n' "${ENV_FILE}"
  printf 'Edit the placeholder secrets and service URLs, then run this script again.\n'
  exit 1
fi

env_value() {
  sed -n "s/^${1}=//p" "${ENV_FILE}" | tail -n 1
}

resolve_path() {
  local value="$1"
  if [[ "${value}" = /* ]]; then
    printf '%s\n' "${value}"
  else
    printf '%s/%s\n' "${PROJECT_DIR}" "${value}"
  fi
}

livetalking_path="${LIVETALKING_PATH:-$(env_value LIVETALKING_PATH)}"
whisperlivekit_path="${WHISPERLIVEKIT_PATH:-$(env_value WHISPERLIVEKIT_PATH)}"
expected_whisperlivekit_commit="$(env_value WHISPERLIVEKIT_COMMIT)"
LIVETALKING_DIR="$(resolve_path "${livetalking_path:-../LiveTalking}")"
WHISPERLIVEKIT_DIR="$(resolve_path "${whisperlivekit_path:-../WhisperLiveKit}")"

placeholder_keys=()
for key in FLASK_SECRET_KEY OPENAI_API_KEY LLM_BASE_URL TTS_BASE_URL; do
  value="$(sed -n "s/^${key}=//p" "${ENV_FILE}" | tail -n 1)"
  if [[ -z "${value}" || "${value}" == replace-* || "${value}" == *replace-with* ]]; then
    placeholder_keys+=("${key}")
  fi
done

if (( ${#placeholder_keys[@]} > 0 )); then
  problem ".env still needs values for: ${placeholder_keys[*]}"
else
  ok "required .env values are configured"
fi

if [[ ! -f "${WHISPERLIVEKIT_DIR}/Dockerfile" ]]; then
  problem "WhisperLiveKit was not found at ${WHISPERLIVEKIT_DIR}"
else
  ok "WhisperLiveKit checkout found"

  if [[ -n "${expected_whisperlivekit_commit}" ]] && command -v git >/dev/null 2>&1; then
    actual_whisperlivekit_commit="$(git -C "${WHISPERLIVEKIT_DIR}" rev-parse HEAD 2>/dev/null || true)"
    if [[ "${actual_whisperlivekit_commit}" != "${expected_whisperlivekit_commit}" ]]; then
      problem "WhisperLiveKit is not at the tested commit ${expected_whisperlivekit_commit}"
    else
      ok "WhisperLiveKit is at the tested commit"
    fi
  fi
fi

if [[ ! -f "${LIVETALKING_DIR}/app.py" ]]; then
  problem "LiveTalking was not found at ${LIVETALKING_DIR}"
else
  ok "LiveTalking checkout found"

  if ! grep -q "close_session" "${LIVETALKING_DIR}/server/routes.py" 2>/dev/null; then
    problem "LiveTalking does not appear to contain the tutor session-cleanup changes"
  else
    ok "LiveTalking tutor integration changes found"
  fi

  if [[ ! -f "${LIVETALKING_DIR}/models/wav2lip.pth" ]]; then
    problem "LiveTalking model is missing: models/wav2lip.pth"
  else
    ok "LiveTalking Wav2Lip model found"
  fi

  if [[ ! -f "${LIVETALKING_DIR}/data/avatars/tutor_avatar_v3/coords.pkl" ]]; then
    problem "LiveTalking avatar is missing: data/avatars/tutor_avatar_v3"
  else
    ok "LiveTalking tutor_avatar_v3 found"
  fi
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  if (cd "${PROJECT_DIR}" && docker compose config --quiet); then
    ok "Docker Compose configuration is valid"
  else
    problem "Docker Compose configuration is invalid"
  fi
fi

if (( errors > 0 )); then
  printf '\nSetup has %d item(s) to resolve. See HANDOFF.md for instructions.\n' "${errors}" >&2
  exit 1
fi

printf '\nLocal prerequisites are ready. Start LiveTalking first, then run:\n'
printf '  cd %s && docker compose up -d --build\n' "${PROJECT_DIR}"
