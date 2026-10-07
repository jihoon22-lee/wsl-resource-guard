#!/usr/bin/env bash
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SOURCE_DIR
cd "$SOURCE_DIR"
exec /usr/bin/python3 -m wsl_resource_guard.guard_install "$@"
