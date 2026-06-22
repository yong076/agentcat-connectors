#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/yong076/agentcat-connectors.git"
ARCHIVE_URL="${AGENTCAT_CONNECTORS_ARCHIVE_URL:-https://github.com/yong076/agentcat-connectors/archive/refs/heads/main.tar.gz}"
INSTALL_DIR="${AGENTCAT_CONNECTORS_DIR:-$HOME/.agentcat/connectors}"
# Optional integrity check for the connector archive. The public auto-update
# path defaults to the mutable tarball, so verification is opt-in to avoid
# breaking existing installs: when a known-good digest is available, the swap
# is gated on it, mirroring scripts/pro_channel_install.py's checksum verify.
#   - AGENTCAT_CONNECTORS_SHA256: 64 lowercase hex digest passed by the caller
#     (e.g. an app-led update once the release pipeline publishes a manifest).
#   - AGENTCAT_CONNECTORS_SHA256_URL: URL of a sidecar digest published next to
#     the tarball. Empty (the default today) keeps current behavior unchanged.
#   - AGENTCAT_CONNECTORS_ARCHIVE_URL: optional pinned archive URL. Empty (the
#     default today) keeps the public main-branch archive path unchanged.
ARCHIVE_SHA256="${AGENTCAT_CONNECTORS_SHA256:-}"
ARCHIVE_SHA256_URL="${AGENTCAT_CONNECTORS_SHA256_URL:-}"

sha256_of() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${file}" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "${file}" | awk '{print $1}'
  else
    return 2
  fi
}

verify_archive_checksum() {
  local archive="$1" expected="${ARCHIVE_SHA256}" actual
  if [[ -z "${expected}" && -n "${ARCHIVE_SHA256_URL}" ]]; then
    expected="$(curl --retry 4 --retry-delay 2 --retry-all-errors -fsSL "${ARCHIVE_SHA256_URL}" 2>/dev/null | tr -d '[:space:]' | head -c 64 || true)"
  fi
  # No digest available: keep current (default) behavior so existing installs
  # are never blocked by an integrity source that has not shipped yet.
  if [[ -z "${expected}" ]]; then
    return 0
  fi
  if [[ ! "${expected}" =~ ^[0-9a-f]{64}$ ]]; then
    printf '[agentcat] connector checksum is not 64 lowercase hex chars; refusing swap.\n' >&2
    return 1
  fi
  if ! actual="$(sha256_of "${archive}")"; then
    printf '[agentcat] no sha256 tool available to verify connector; refusing swap.\n' >&2
    return 1
  fi
  if [[ "${actual}" != "${expected}" ]]; then
    printf '[agentcat] connector checksum mismatch; refusing swap.\n' >&2
    return 1
  fi
}

install_from_archive() {
  local tmp_dir archive extracted
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/agentcat-connectors.XXXXXX")"
  archive="${tmp_dir}/agentcat-connectors.tar.gz"
  if ! curl --retry 4 --retry-delay 2 --retry-all-errors -fsSL "${ARCHIVE_URL}" -o "${archive}"; then
    rm -rf "${tmp_dir}"
    return 1
  fi
  if ! verify_archive_checksum "${archive}"; then
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

  if [[ -n "${AGENTCAT_CONNECTORS_ARCHIVE_URL:-}" ]]; then
    mkdir -p "$(dirname "${INSTALL_DIR}")"
    if ! install_from_archive >&2; then
      printf '[agentcat] pinned connector archive install failed; refusing mutable fallback.\n' >&2
      return 1
    fi
    printf '%s\n' "${INSTALL_DIR}"
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
