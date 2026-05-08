#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/yong076/agentcat-connectors.git"
ARCHIVE_URL="https://github.com/yong076/agentcat-connectors/archive/refs/heads/main.tar.gz"
INSTALL_DIR="${AGENTCAT_CONNECTORS_DIR:-$HOME/.agentcat/connectors}"

install_from_archive() {
  local tmp_dir archive extracted
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/agentcat-connectors.XXXXXX")"
  archive="${tmp_dir}/agentcat-connectors.tar.gz"
  if ! curl --retry 4 --retry-delay 2 --retry-all-errors -fsSL "${ARCHIVE_URL}" -o "${archive}"; then
    rm -rf "${tmp_dir}"
    return 1
  fi
  tar -xzf "${archive}" -C "${tmp_dir}"
  extracted="$(find "${tmp_dir}" -maxdepth 1 -type d -name 'agentcat-connectors-*' | head -n 1)"
  if [[ -z "${extracted}" || ! -f "${extracted}/scripts/install.py" || ! -f "${extracted}/bin/agentcat" ]]; then
    rm -rf "${tmp_dir}"
    return 1
  fi
  rm -rf "${INSTALL_DIR}"
  mkdir -p "$(dirname "${INSTALL_DIR}")"
  mv "${extracted}" "${INSTALL_DIR}"
  rm -rf "${tmp_dir}"
}

resolve_repo_dir() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd -P || true)"
  if [[ -n "${script_dir}" && -f "${script_dir}/scripts/install.py" && -f "${script_dir}/bin/agentcat" ]]; then
    printf '%s\n' "${script_dir}"
    return 0
  fi

  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    if ! git -C "${INSTALL_DIR}" pull --ff-only >&2; then
      printf '[agentcat] GitHub update failed; using existing local connector copy.\n' >&2
    fi
    printf '%s\n' "${INSTALL_DIR}"
    return 0
  fi

  if [[ -f "${INSTALL_DIR}/scripts/install.py" && -f "${INSTALL_DIR}/bin/agentcat" ]]; then
    if ! install_from_archive >&2; then
      printf '[agentcat] GitHub archive update failed; using existing local connector copy.\n' >&2
    fi
    printf '%s\n' "${INSTALL_DIR}"
    return 0
  fi

  mkdir -p "$(dirname "${INSTALL_DIR}")"
  if ! git clone --depth 1 --filter=blob:none "${REPO_URL}" "${INSTALL_DIR}" >&2; then
    printf '[agentcat] git clone failed; retrying with GitHub archive download.\n' >&2
    install_from_archive >&2
  fi
  printf '%s\n' "${INSTALL_DIR}"
}

REPO_DIR="$(resolve_repo_dir | tail -n 1)"
python3 "${REPO_DIR}/scripts/install.py" --repo-dir "${REPO_DIR}" install
