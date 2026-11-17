#!/usr/bin/env bash
# =============================================================================
# deploy/install.sh — CU-Rates.org Deployment Installer
#
# Idempotent installer for Raspberry Pi 5 (Debian 13 trixie arm64).
# Defaults to --dry-run. Refuses to make any system changes unless --apply is passed.
# =============================================================================

set -euo pipefail

DEFAULT_NVME_ROOT="/var/lib/cu-rates"
NVME_ROOT="${CU_RATES_ROOT:-$DEFAULT_NVME_ROOT}"
APP_USER="cu-rates"
APP_GROUP="cu-rates"
CONFIG_DIR="/etc/cu-rates"
SYSTEMD_DIR="/etc/systemd/system"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

APPLY=0
DRY_RUN=1
ADOPT=0

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Idempotent installer for CU-Rates.org on Raspberry Pi 5.
Defaults to --dry-run. Refuses to apply any changes without --apply.

Options:
    --dry-run   Simulate execution and print intended actions (default).
    --apply     Actually apply configuration and create filesystem layout.
    --adopt     Proceed when CU_RATES_ROOT exists and is not empty without recursive chown.
    --help      Show this help message.

Environment:
    CU_RATES_ROOT   Base directory on NVMe SSD (default: ${DEFAULT_NVME_ROOT}).
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            APPLY=0
            DRY_RUN=1
            shift
            ;;
        --apply)
            APPLY=1
            DRY_RUN=0
            shift
            ;;
        --adopt)
            ADOPT=1
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Error: Unknown option '$1'" >&2
            usage >&2
            exit 1
            ;;
    esac
done

run_action() {
    local desc="$1"
    shift
    if [[ "${APPLY}" -eq 1 ]]; then
        echo "[APPLY] ${desc}"
        "$@"
    else
        echo "[DRY-RUN] Would ${desc}"
    fi
}

echo "=== CU-Rates.org Deployment Bundle Installer ==="
if [[ "${APPLY}" -eq 1 ]]; then
    echo "Mode: APPLY (live execution)"
else
    echo "Mode: DRY-RUN (no changes will be made; pass --apply to execute)"
fi
echo "Target root: ${NVME_ROOT}"
echo "Dedicated user: ${APP_USER}:${APP_GROUP}"
echo "================================================"

# 1. Guard against invalid, SD-card, or cloud-synchronized paths (MASTER.md §7, D-07, D-10)
validate_path() {
    local target="$1"
    case "${target}" in
        *"Mobile Documents"*|*"iCloud"*|*"Dropbox"*|*"OneDrive"*)
            echo "Error: Path '${target}' appears to be a cloud-synchronized folder. Exiting per MASTER.md §7." >&2
            exit 1
            ;;
        *"/media/"*|*"/mnt/sd"*|*"/mnt/mmc"*)
            echo "Error: Path '${target}' appears to be on removable/SD media. NVMe SSD required per MASTER.md D-07." >&2
            exit 1
            ;;
    esac
}
validate_path "${NVME_ROOT}"

# Guard against existing non-empty root directory unless --adopt is given
if [[ -d "${NVME_ROOT}" ]] && [[ "${ADOPT}" -ne 1 ]]; then
    if [[ -n "$(ls -A "${NVME_ROOT}" 2>/dev/null)" ]]; then
        echo "Error: Root directory '${NVME_ROOT}' exists and is not empty. Refusing to proceed unless --adopt is specified." >&2
        exit 1
    fi
fi

# 2. User & group creation (unprivileged, system service account)
create_user() {
    if id "${APP_USER}" >/dev/null 2>&1; then
        echo "User '${APP_USER}' already exists."
    else
        run_action "create system user '${APP_USER}'" \
            useradd --system --shell /usr/sbin/nologin --home-dir "${NVME_ROOT}" "${APP_USER}"
    fi
}
create_user

# 3. Create disk layout under single NVMe root
# Only chown directories that were created; never recursively chown an existing directory.
DIRS=(
    "${NVME_ROOT}"
    "${NVME_ROOT}/app"
    "${NVME_ROOT}/venv"
    "${NVME_ROOT}/.cache"
    "${NVME_ROOT}/data"
    "${NVME_ROOT}/artifacts"
    "${NVME_ROOT}/snapshots"
    "${NVME_ROOT}/snapshots/versions"
    "${NVME_ROOT}/backups"
    "${CONFIG_DIR}"
    "/run/cu-rates-web"
    "/run/cu-rates-worker"
)

for dir in "${DIRS[@]}"; do
    if [[ -d "${dir}" ]]; then
        echo "Directory '${dir}' already exists."
    else
        run_action "create directory '${dir}'" mkdir -p "${dir}"
        run_action "set ownership of created directory '${dir}' to ${APP_USER}:${APP_GROUP}" \
            chown "${APP_USER}:${APP_GROUP}" "${dir}"
    fi
done

# Restrictive permissions on data directories (fail loudly in apply mode)
run_action "set 0750 permissions on data directories" \
    chmod 0750 "${NVME_ROOT}/data" "${NVME_ROOT}/artifacts" "${NVME_ROOT}/backups"

# 4. Install environment template if /etc/cu-rates/cu-rates.env does not exist
ENV_FILE="${CONFIG_DIR}/cu-rates.env"
if [[ -f "${ENV_FILE}" ]]; then
    echo "Configuration '${ENV_FILE}' already exists (not overwriting)."
else
    run_action "install environment file template to '${ENV_FILE}' (mode 0600)" \
        install -m 0600 -o "${APP_USER}" -g "${APP_GROUP}" "${SCRIPT_DIR}/env.example" "${ENV_FILE}"
fi

# 5. Install systemd units
UNITS=(
    "cu-rates-web.service"
    "cu-rates-worker.service"
    "cu-rates-worker.timer"
)

for unit in "${UNITS[@]}"; do
    src="${SCRIPT_DIR}/systemd/${unit}"
    dst="${SYSTEMD_DIR}/${unit}"
    if [[ -f "${src}" ]]; then
        run_action "install systemd unit '${unit}' to '${SYSTEMD_DIR}'" \
            install -m 0644 "${src}" "${dst}"
    fi
done

# 6. Service activation policy (D-15)
# Web service: enabled to start on boot
run_action "reload systemd daemon" \
    systemctl daemon-reload

run_action "enable cu-rates-web.service (starts on boot)" \
    systemctl enable cu-rates-web.service

# Worker service and timer: INSTALLED DISABLED per MASTER.md decision D-15
run_action "ensure cu-rates-worker.service is disabled (D-15)" \
    systemctl disable cu-rates-worker.service

run_action "ensure cu-rates-worker.timer is disabled (D-15)" \
    systemctl disable cu-rates-worker.timer

# 7. Safety check: never touch existing host services
echo "Notice: Existing host services, Caddy sites, and other system units were NOT modified."

if [[ "${APPLY}" -eq 1 ]]; then
    echo "=== Installation Applied Successfully ==="
    echo "Next steps for operator:"
    echo "1. Review and populate /etc/cu-rates/cu-rates.env"
    echo "2. Clone app repository as service user: sudo -u cu-rates git clone https://github.com/aisetsyoufree/curatesv2.git ${NVME_ROOT}/app"
    echo "3. Build venv using: sudo -u cu-rates UV_PROJECT_ENVIRONMENT=${NVME_ROOT}/venv UV_CACHE_DIR=${NVME_ROOT}/.cache/uv uv sync --locked --no-dev --group deploy"
    echo "4. Configure Caddy: copy deploy/caddy/Caddyfile to /etc/caddy/conf.d/cu-rates.caddyfile"
    echo "5. Start web service: sudo systemctl start cu-rates-web.service"
    echo "6. Note: Worker timer remains DISABLED per MASTER.md D-15."
else
    echo "=== Dry-Run Complete ==="
    echo "No filesystem, user, or service changes were made."
    echo "To execute this plan, re-run with: $0 --apply"
fi
