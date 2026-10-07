#!/usr/bin/env bash
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ${EUID} -ne 0 ]]; then
  exec sudo /bin/bash "$SOURCE_DIR/install-services.sh" --owner "$(id -un)" "$@"
fi
readonly SOURCE_DIR
cd "$SOURCE_DIR"
exec /usr/bin/python3 -m wsl_resource_guard.service_install "$@"
