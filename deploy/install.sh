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
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
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

# 1. Guard against invalid, SD-card, cloud-synchronized, or unsafe paths (MASTER.md §7, D-07, D-10)
validate_path() {
    local target="$1"
    if [[ -z "${target}" || "${target}" != /* ]]; then
        echo "Error: Path '${target}' must be a non-empty absolute path." >&2
        exit 1
    fi
    case "${target}" in
        *"/.."*|*"../"*)
            echo "Error: Path '${target}' contains path traversal ('..'). Exiting." >&2
            exit 1
            ;;
        *"Mobile Documents"*|*"iCloud"*|*"Dropbox"*|*"OneDrive"*)
            echo "Error: Path '${target}' appears to be a cloud-synchronized folder. Exiting per MASTER.md §7." >&2
            exit 1
            ;;
        *"/media/"*|*"/mnt/sd"*|*"/mnt/mmc"*)
            echo "Error: Path '${target}' appears to be on removable/SD media. NVMe SSD required per MASTER.md D-07." >&2
            exit 1
            ;;
        "/"|"/etc"|"/etc/"*|"/usr"|"/usr/"*|"/bin"|"/bin/"*|"/sbin"|"/sbin/"*|"/boot"|"/boot/"*|"/dev"|"/dev/"*|"/proc"|"/proc/"*|"/sys"|"/sys/"*|"/root"|"/root/"*|"/var"|"/run"|"/home"|"/tmp"|"/private/etc"|"/private/etc/"*|"/private/var")
            echo "Error: Path '${target}' is an unsafe root directory. Exiting." >&2
            exit 1
            ;;
    esac
    if [[ -L "${target}" || -h "${target}" ]]; then
        echo "Error: Path '${target}' is a symlink. Symlinks are forbidden." >&2
        exit 1
    fi
    if [[ -e "${target}" && ! -d "${target}" ]]; then
        echo "Error: Path '${target}' exists but is not a real directory." >&2
        exit 1
    fi
}
validate_path "${NVME_ROOT}"

confirm_real_directory() {
    local target="$1"
    if [[ -L "${target}" || -h "${target}" ]]; then
        echo "Error: Backing path '${target}' is a symlink. Real directory required." >&2
        exit 1
    fi
    if [[ ! -d "${target}" ]]; then
        echo "Error: Backing path '${target}' is not a real directory." >&2
        exit 1
    fi
}

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
    "${NVME_ROOT}/intake"
    "${NVME_ROOT}/automatic"
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

run_action "confirm backing path '${NVME_ROOT}' is a real directory" \
    confirm_real_directory "${NVME_ROOT}"

# Operator-owned directory for independent withdrawal/denylist ledger (mode 0755, root/operator owned)
INDEPENDENT_DIR="${NVME_ROOT}/independent"
if [[ -d "${INDEPENDENT_DIR}" ]]; then
    echo "Directory '${INDEPENDENT_DIR}' already exists."
else
    run_action "create operator-owned independent directory '${INDEPENDENT_DIR}'" \
        mkdir -p "${INDEPENDENT_DIR}"
    run_action "set ownership of independent directory '${INDEPENDENT_DIR}' to root" \
        chown root "${INDEPENDENT_DIR}"
fi
run_action "set 0755 permissions on independent directory '${INDEPENDENT_DIR}'" \
    chmod 0755 "${INDEPENDENT_DIR}"

verify_operator_directory() {
    local target="$1"
    if id -u >/dev/null 2>&1 && [[ "$(id -u)" == "0" ]]; then
        local actual_owner
        actual_owner="$(stat -c '%U' "${target}" 2>/dev/null || stat -f '%Su' "${target}" 2>/dev/null)"
        if [[ "${actual_owner}" == "${APP_USER}" ]]; then
            echo "Error: Independent directory '${target}' must not be owned by service user '${APP_USER}'." >&2
            exit 1
        fi
    fi
}
run_action "verify independent directory '${INDEPENDENT_DIR}' is not owned by service user" \
    verify_operator_directory "${INDEPENDENT_DIR}"

# Restrictive permissions on data and backup directories (fail loudly in apply mode)
run_action "set 0750 permissions on data and backup directories" \
    chmod 0750 "${NVME_ROOT}/data" "${NVME_ROOT}/backups"

# The correction inbox directory must be private (the store refuses a group/world-writable parent)
run_action "set 0700 permissions on the correction inbox directory" \
    chmod 0700 "${NVME_ROOT}/intake"

# Artifact storage must be mode 0700 (ArtifactStore strictly rejects group/other access)
run_action "set 0700 permissions on artifact storage directory" \
    chmod 0700 "${NVME_ROOT}/artifacts"

verify_artifact_ownership() {
    local target="$1"
    local expected_user="$2"
    if id -u >/dev/null 2>&1 && [[ "$(id -u)" == "0" ]]; then
        local actual_user
        actual_user="$(stat -c '%U' "${target}" 2>/dev/null || stat -f '%Su' "${target}" 2>/dev/null)"
        if [[ -n "${actual_user}" && "${actual_user}" != "${expected_user}" ]]; then
            echo "Error: Artifact store '${target}' is owned by '${actual_user}', must be owned by '${expected_user}'." >&2
            exit 1
        fi
    fi
}
run_action "verify ownership of '${NVME_ROOT}/artifacts' by ${APP_USER}" \
    verify_artifact_ownership "${NVME_ROOT}/artifacts" "${APP_USER}"

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
    "cu-rates-backup.service"
    "cu-rates-backup.timer"
    "cu-rates-health.service"
    "cu-rates-health.timer"
    "cu-rates-intake-purge.service"
    "cu-rates-intake-purge.timer"
)

for unit in "${UNITS[@]}"; do
    src="${SCRIPT_DIR}/systemd/${unit}"
    dst="${SYSTEMD_DIR}/${unit}"
    if [[ -f "${src}" ]]; then
        run_action "install systemd unit '${unit}' to '${SYSTEMD_DIR}'" \
            install -m 0644 "${src}" "${dst}"
    fi
done

# 6. Service activation policy (D-15, D-17)
# Web service: enabled to start on boot
run_action "reload systemd daemon" \
    systemctl daemon-reload

run_action "enable cu-rates-web.service (starts on boot)" \
    systemctl enable cu-rates-web.service

# Worker service and timer: STOPPED, DISABLED, and MASKED per MASTER.md D-15 and D-17
run_action "stop cu-rates-worker.service (D-15, D-17)" \
    systemctl stop cu-rates-worker.service

run_action "stop cu-rates-worker.timer (D-15, D-17)" \
    systemctl stop cu-rates-worker.timer

run_action "ensure cu-rates-worker.service is disabled (D-15)" \
    systemctl disable cu-rates-worker.service

run_action "ensure cu-rates-worker.timer is disabled (D-15)" \
    systemctl disable cu-rates-worker.timer

run_action "mask cu-rates-worker.service (D-15, D-17)" \
    ln -sf /dev/null "${SYSTEMD_DIR}/cu-rates-worker.service"

run_action "mask cu-rates-worker.timer (D-15, D-17)" \
    ln -sf /dev/null "${SYSTEMD_DIR}/cu-rates-worker.timer"

run_action "reload systemd daemon after masking" \
    systemctl daemon-reload

# 7. Record installed git commit in root-owned state file (D-17)
COMMIT_FILE="${NVME_ROOT}/installed-commit"
GIT_COMMIT="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo "unknown")"

record_installed_commit() {
    local target="$1"
    local commit="$2"
    printf "%s\n" "${commit}" > "${target}"
    chmod 0644 "${target}"
    if id -u >/dev/null 2>&1 && [[ "$(id -u)" == "0" ]]; then
        chown root "${target}"
    fi
}
run_action "record installed git commit '${GIT_COMMIT}' to '${COMMIT_FILE}' (mode 0644, D-17)" \
    record_installed_commit "${COMMIT_FILE}" "${GIT_COMMIT}"

# 8. Safety check: never touch existing host services
echo "Notice: Existing host services, Caddy sites, and other system units were NOT modified."

if [[ "${APPLY}" -eq 1 ]]; then
    echo "=== Installation Applied Successfully ==="
    echo "Next steps for operator:"
    echo "1. Review and populate /etc/cu-rates/cu-rates.env"
    echo "2. Clone app repository as service user: sudo -u cu-rates git clone https://github.com/aisetsyoufree/curatesv2.git ${NVME_ROOT}/app"
    echo "3. Build venv using: sudo -u cu-rates UV_PROJECT_ENVIRONMENT=${NVME_ROOT}/venv UV_CACHE_DIR=${NVME_ROOT}/.cache/uv uv sync --locked --no-dev --group deploy"
    echo "4. Configure Caddy: copy deploy/caddy/Caddyfile to /etc/caddy/conf.d/cu-rates.caddyfile"
    echo "5. Start web service: sudo systemctl start cu-rates-web.service"
    echo "6. Note: Worker service and timer remain STOPPED, DISABLED, and MASKED per MASTER.md D-15 and D-17."
    echo "7. Enable scheduled operations (backup and health timers): sudo systemctl enable --now cu-rates-backup.timer cu-rates-health.timer"
    echo "8. Correction inbox: assign a person to monitor it, run 'sudo -u cu-rates ${NVME_ROOT}/venv/bin/python -m cu_rates.intake --db ${NVME_ROOT}/intake/intake.sqlite init', set INTAKE_DB in ${ENV_FILE}, restart cu-rates-web, then sudo systemctl enable --now cu-rates-intake-purge.timer"
else
    echo "=== Dry-Run Complete ==="
    echo "No filesystem, user, or service changes were made."
    echo "To execute this plan, re-run with: $0 --apply"
fi
