#!/usr/bin/env bash
set -euo pipefail
if [ "${EUID}" -ne 0 ]; then
  printf '%s\n' 'Run this installer with sudo on the Docker host.' >&2
  exit 1
fi
if ! command -v apparmor_parser >/dev/null; then
  printf '%s\n' 'AppArmor parser is unavailable; ask the host administrator to configure runner isolation.' >&2
  exit 1
fi
profile_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)/deploy/apparmor/ai-factory-sandbox"
install -m 0644 "$profile_source" /etc/apparmor.d/ai-factory-sandbox
apparmor_parser -r /etc/apparmor.d/ai-factory-sandbox
printf '%s\n' 'Loaded the ai-factory-sandbox profile. Global AppArmor/user-namespace restrictions were not changed.'
