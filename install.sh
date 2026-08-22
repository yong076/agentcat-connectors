#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${AGENTCAT_CONNECTORS_DIR:-$HOME/.agentcat/connectors}"
BACKUP_ROOT="${AGENTCAT_CONNECTORS_BACKUP_ROOT:-$HOME/.agentcat/backups/connector-source}"
MANIFEST_URL="${AGENTCAT_CONNECTORS_MANIFEST_URL:-https://github.com/yong076/agentcat-connectors/releases/latest/download/connector-manifest.json}"

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
  local archive="$1" expected="$2" actual
  if [[ ! "${expected}" =~ ^[0-9a-f]{64}$ ]]; then
    printf '[agentcat] connector checksum is not 64 lowercase hex chars; refusing install.\n' >&2
    return 1
  fi
  if ! actual="$(sha256_of "${archive}")"; then
    printf '[agentcat] no sha256 tool available; refusing install.\n' >&2
    return 1
  fi
  if [[ "${actual}" != "${expected}" ]]; then
    printf '[agentcat] connector checksum mismatch; existing connector was not touched.\n' >&2
    return 1
  fi
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd -P || true)"
install_dir_abs="$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "${INSTALL_DIR}")"
if [[ -n "${script_dir}" && -f "${script_dir}/scripts/install.py" && -f "${script_dir}/bin/agentcat" && "${script_dir}" != "${install_dir_abs}" ]]; then
  printf '[agentcat] Installing from development checkout %s\n' "${script_dir}"
  exec python3 "${script_dir}/scripts/install.py" --repo-dir "${script_dir}" install
fi

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/agentcat-connectors.XXXXXX")"
trap 'rm -rf "${tmp_dir}"' EXIT
manifest_path="${tmp_dir}/connector-manifest.json"
archive_path="${tmp_dir}/agentcat-connectors.zip"
extract_path="${tmp_dir}/extracted"

if [[ -n "${AGENTCAT_CONNECTORS_ARCHIVE_URL:-}" ]]; then
  if [[ -z "${AGENTCAT_CONNECTORS_SHA256:-}" || -z "${AGENTCAT_CONNECTORS_VERSION:-}" ]]; then
    printf '[agentcat] pinned archive installs require AGENTCAT_CONNECTORS_SHA256 and AGENTCAT_CONNECTORS_VERSION.\n' >&2
    exit 1
  fi
  python3 - "${manifest_path}" <<'PY'
import json, os, sys
payload = {
    "version": os.environ["AGENTCAT_CONNECTORS_VERSION"],
    "contractVersion": int(os.environ.get("AGENTCAT_CONNECTORS_CONTRACT_VERSION") or "1"),
    "archiveUrl": os.environ["AGENTCAT_CONNECTORS_ARCHIVE_URL"],
    "sha256": os.environ["AGENTCAT_CONNECTORS_SHA256"],
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
PY
else
  curl --retry 4 --retry-delay 2 --retry-all-errors -fsSL "${MANIFEST_URL}" -o "${manifest_path}"
fi

version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "${manifest_path}")"
contract_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["contractVersion"])' "${manifest_path}")"
archive_url="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["archiveUrl"])' "${manifest_path}")"
archive_sha256="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["sha256"])' "${manifest_path}")"

if [[ ! "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ || ! "${contract_version}" =~ ^[1-9][0-9]*$ ]]; then
  printf '[agentcat] connector manifest version fields are invalid.\n' >&2
  exit 1
fi
if [[ ! "${archive_url}" =~ ^https://github\.com/yong076/agentcat-connectors/ ]]; then
  printf '[agentcat] connector manifest archive URL is not approved.\n' >&2
  exit 1
fi

printf '[agentcat] Downloading verified connector %s\n' "${version}"
curl --retry 4 --retry-delay 2 --retry-all-errors -fsSL "${archive_url}" -o "${archive_path}"
verify_archive_checksum "${archive_path}" "${archive_sha256}"

mkdir -p "${extract_path}"
python3 -m zipfile -e "${archive_path}" "${extract_path}"
candidate="$(find "${extract_path}" -maxdepth 1 -type d -name 'agentcat-connectors-*' | head -n 1)"
if [[ -z "${candidate}" || ! -f "${candidate}/scripts/public_channel_install.py" || ! -f "${candidate}/contracts/connector-v1.json" ]]; then
  printf '[agentcat] verified archive does not contain the public channel installer.\n' >&2
  exit 1
fi

python3 "${candidate}/scripts/public_channel_install.py" \
  --archive "${archive_path}" \
  --manifest "${manifest_path}" \
  --install-dir "${INSTALL_DIR}" \
  --backup-root "${BACKUP_ROOT}"
