#!/usr/bin/env bash
set -euo pipefail

# Uninstall uses local source and retains usage/backups. Never download, pull
# or replace an existing checkout just to remove service and hook registration.
INSTALL_DIR="${AGENTCAT_CONNECTORS_DIR:-$HOME/.agentcat/connectors}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd -P || true)"
if [[ -f "${INSTALL_DIR}/scripts/install.py" ]]; then
  repo_dir="${INSTALL_DIR}"
elif [[ -n "${script_dir}" && -f "${script_dir}/scripts/install.py" ]]; then
  repo_dir="${script_dir}"
else
  printf '%s\n' '[agentcat] Local connector source was not found. Set AGENTCAT_CONNECTORS_DIR to its location.' >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  printf '%s\n' '[agentcat] Python 3.9 or newer is required to uninstall the connector.' >&2
  exit 1
fi
exec python3 "${repo_dir}/scripts/install.py" --repo-dir "${repo_dir}" uninstall
