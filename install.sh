#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/yong076/agentcat-connectors.git"
INSTALL_DIR="${AGENTCAT_CONNECTORS_DIR:-$HOME/.agentcat/connectors}"

resolve_repo_dir() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd -P || true)"
  if [[ -n "${script_dir}" && -f "${script_dir}/scripts/install.py" && -f "${script_dir}/bin/agentcat" ]]; then
    printf '%s\n' "${script_dir}"
    return 0
  fi

  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    git -C "${INSTALL_DIR}" pull --ff-only >&2
  else
    mkdir -p "$(dirname "${INSTALL_DIR}")"
    git clone "${REPO_URL}" "${INSTALL_DIR}" >&2
  fi
  printf '%s\n' "${INSTALL_DIR}"
}

REPO_DIR="$(resolve_repo_dir)"
python3 "${REPO_DIR}/scripts/install.py" --repo-dir "${REPO_DIR}" install
