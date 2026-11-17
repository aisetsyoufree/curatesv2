# CU-Rates.org V2 — Raspberry Pi 5 Deployment Runbook

> **STATUS: UNEXECUTED DEPLOYMENT BUNDLE**  
> This deployment bundle and runbook have been prepared for review and verification.  
> **Nothing has been executed on the target host.**  
>
> **REQUIRED OWNER APPROVALS BEFORE LIVE EXECUTION:**
> 1. **Product Owner Approval:** Formal review and approval of the deployment plan and target environment.
> 2. **Host Review:** Review of the existing services currently running on the Raspberry Pi 5 to confirm isolated port allocations (127.0.0.1:8080 for Caddy reverse proxy, 127.0.0.1:8000 for Gunicorn), dedicated service user (`cu-rates`), and NVMe filesystem capacity.
> 3. **Counsel / Policy Clearance:** Verification of data collection and terms boundaries per `MASTER.md` §5 before any collection enablement.
> 4. **Worker Transport Restriction (Decision D-15):** The worker service and timer MUST remain **INSTALLED DISABLED**. Live collection is prohibited until the gateway pinned dialer (issue #68) is implemented, reviewed, and approved.
> 5. **Cloudflare Tunnel Routing:** Verification of the `cloudflared` tunnel ingress configuration routing `cu-rates.org` to the local Caddy reverse proxy on loopback port 8080 (`http://127.0.0.1:8080`) without inbound router ports (`MASTER.md` D-09). Caddy in turn proxies traffic to Gunicorn on `127.0.0.1:8000`.

---

## 1. Prerequisites

- **Host Hardware:** Raspberry Pi 5 with 16 GB RAM.
- **Operating System:** Debian 13 (*trixie*) arm64 / Raspberry Pi OS 64-bit (Debian 13 base).
- **Storage Subsystem:** Dedicated NVMe SSD root filesystem (M.2 NVMe HAT).  
  *SD-card runtime storage and cloud-synchronized directories (iCloud, Dropbox, OneDrive) are strictly prohibited per `MASTER.md` §7, D-07, and D-10.*
- **System Software:**
  - `systemd` (init and service supervisor)
  - Python 3.12 managed via `uv` (`.python-version` pinned)
  - `caddy` (version 2.7+ HTTP reverse proxy)
  - `cloudflared` (Cloudflare Tunnel daemon, edge-authenticated)
  - `git`
- **Host Co-existence:** Other services already run on this Pi 5. CU-Rates runs under an isolated unprivileged user (`cu-rates`) and binds strictly to loopback (`127.0.0.1:8080` for Caddy and `127.0.0.1:8000` for Gunicorn), ensuring zero port collisions and zero interference with host services.

---

## 2. Disk Layout and Security Architecture

All persistent data, code, virtual environments, and evidence artifacts reside under a single NVMe root (`/var/lib/cu-rates`):

```
/var/lib/cu-rates/
├── app/                  # Application code repository clone
├── venv/                 # uv-managed Python 3.12 virtual environment (deploy group)
├── snapshots/            # Public snapshot directory (read-only for web process)
│   ├── pointer.json      # Atomic JSON pointer to current active snapshot version
│   ├── denylist.json     # Emergency withdrawal denylist
│   └── versions/         # Verified, immutable public SQLite databases
├── data/                 # Operational SQLite database (WAL mode; worker only, mode 0750)
├── artifacts/            # Content-addressed raw evidence store (worker only, mode 0750)
└── backups/              # Encrypted backup staging area (mode 0750)

/etc/cu-rates/
└── cu-rates.env          # Externalized environment configuration (mode 0600)

/run/cu-rates-web/        # Ephemeral web runtime tmpfs directory (mode 0755)
/run/cu-rates-worker/     # Ephemeral worker runtime tmpfs directory (mode 0750)
```

### Architectural Isolation Principles (`MASTER.md` §6, §7)
- **Separation of Web and Worker:** The web service process (`cu-rates-web.service`) **NEVER opens or references the worker database** (`/var/lib/cu-rates/data`). The web process reads only verified public snapshots (`/var/lib/cu-rates/snapshots/`) and static application code. Inaccessible paths are enforced in systemd (`InaccessiblePaths=/var/lib/cu-rates/data /var/lib/cu-rates/artifacts /var/lib/cu-rates/backups`) and `StateDirectory` is omitted from the web unit so it cannot write application data.
- **Unprivileged Execution:** All services execute as the dedicated system user `cu-rates` (group `cu-rates`) with no interactive shell (`/usr/sbin/nologin`).
- **Hardened Sandboxing:** Systemd units enforce `ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp=true`, `NoNewPrivileges=true`, `PrivateDevices=true`, `ProtectClock=true`, `ProtectHostname=true`, `ProtectProc=invisible`, `ProcSubset=pid`, `CapabilityBoundingSet=` (empty), `AmbientCapabilities=` (empty), `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, `SystemCallArchitectures=native`, `SystemCallFilter=@system-service`, `UMask=0077`, kernel tunables protection, strict `ReadWritePaths`, and resource quotas (`MemoryMax`, `TasksMax`, `CPUQuota`).

---

## 3. Installation Runbook

The deployment bundle provides an idempotent installer `deploy/install.sh`.  
It defaults to `--dry-run` and refuses to make changes unless `--apply` is passed.

### Step 1: Clone Repository
Clone the repository to the NVMe root location:
```bash
sudo git clone https://github.com/aisetsyoufree/curatesv2.git /var/lib/cu-rates/app
cd /var/lib/cu-rates/app
```

### Step 2: Preview Installation (Dry-Run)
Inspect the planned actions without modifying the system (pass `--adopt` because Step 1 cloned into `/var/lib/cu-rates/app`, creating a non-empty root):
```bash
sudo bash deploy/install.sh --dry-run --adopt
```

### Step 3: Apply Filesystem and Service Setup
Execute idempotent provisioning adopting the existing repository tree:
```bash
sudo bash deploy/install.sh --apply --adopt
```
*(Passing `--adopt` instructs `install.sh` to safely adopt `/var/lib/cu-rates` and its existing `/var/lib/cu-rates/app` clone without failing on the non-empty root guard).*

This step:
- Creates the unprivileged `cu-rates` user and group.
- Creates the layout under `/var/lib/cu-rates` with safe ownership and permissions (chowning only what is created; never running `chown -R` on existing directories).
- Copies `deploy/env.example` to `/etc/cu-rates/cu-rates.env` (mode 0600) if not present.
- Installs `cu-rates-web.service`, `cu-rates-worker.service`, and `cu-rates-worker.timer` into `/etc/systemd/system/`.
- Enables `cu-rates-web.service` on boot.
- **Explicitly ensures `cu-rates-worker.service` and `cu-rates-worker.timer` remain DISABLED per Decision D-15.**

### Step 4: Provision Virtual Environment
Install production dependencies including Gunicorn into the isolated venv at `/var/lib/cu-rates/venv`:
```bash
cd /var/lib/cu-rates/app
sudo -u cu-rates UV_PROJECT_ENVIRONMENT=/var/lib/cu-rates/venv uv sync --locked --no-dev --group deploy
```

### Step 5: Configure Environment
Review and edit `/etc/cu-rates/cu-rates.env`:
```bash
sudo nano /etc/cu-rates/cu-rates.env
# Ensure mode 0600 is retained:
sudo chmod 0600 /etc/cu-rates/cu-rates.env
```
> **Note on Environment Variables:** The Flask application in `src/cu_rates/web/` does NOT read environment variables today (`create_app` takes an explicit configuration dictionary). Therefore, application variables in `/etc/cu-rates/cu-rates.env` (such as `CANONICAL_ORIGIN`, `ENABLE_ANALYTICS`, `CU_RATES_SNAPSHOT_DIR`) currently have no effect until environment-to-config wiring is implemented in task T055. Only `GUNICORN_CMD_ARGS` is read by Gunicorn directly.

### Step 6: Configure Caddy Reverse Proxy & Cloudflare Tunnel
1. Integrate `deploy/caddy/Caddyfile` into Caddy's configuration:
```bash
sudo cp deploy/caddy/Caddyfile /etc/caddy/conf.d/cu-rates.caddyfile
sudo systemctl reload caddy
```
Caddy listens on loopback plain HTTP port 8080 (`http://127.0.0.1:8080`) with `auto_https off` and proxies upstream to Gunicorn on `127.0.0.1:8000`. Caddy access logging is discarded (`output discard`) to prevent query strings from being logged; Gunicorn handles query-free access logging.

2. In Cloudflare Tunnel (`cloudflared` configuration), configure ingress to route `cu-rates.org` to Caddy at `http://127.0.0.1:8080`:
```yaml
ingress:
  - hostname: cu-rates.org
    service: http://127.0.0.1:8080
  - hostname: www.cu-rates.org
    service: http://127.0.0.1:8080
  - service: http_status:404
```

### Step 7: Start Web Service
Start the web application:
```bash
sudo systemctl start cu-rates-web.service
sudo systemctl status cu-rates-web.service
```

---

## 4. Upgrade Runbook

Upgrades preserve operational state and snapshot data.

1. **Pull Latest Release:**
   ```bash
   cd /var/lib/cu-rates/app
   sudo -u cu-rates git fetch --tags
   sudo -u cu-rates git checkout <target-tag-or-sha>
   ```

2. **Update Virtual Environment:**
   ```bash
   sudo -u cu-rates UV_PROJECT_ENVIRONMENT=/var/lib/cu-rates/venv uv sync --locked --no-dev --group deploy
   ```

3. **Reload Systemd Units (if changed):**
   ```bash
   sudo cp deploy/systemd/* /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

4. **Graceful Reload of Web Service:**
   Gunicorn supports zero-downtime reloads on `SIGHUP`:
   ```bash
   sudo systemctl reload cu-rates-web.service
   ```
   Or clean restart:
   ```bash
   sudo systemctl restart cu-rates-web.service
   ```

5. **Verify Health:**
   ```bash
   curl -s -i http://127.0.0.1:8000/ | head -n 20
   ```

---

## 5. Rollback Runbook

### Code Rollback
To roll back application code to a prior commit:
```bash
cd /var/lib/cu-rates/app
sudo -u cu-rates git checkout <previous-tag-or-sha>
sudo -u cu-rates UV_PROJECT_ENVIRONMENT=/var/lib/cu-rates/venv uv sync --locked --no-dev --group deploy
sudo systemctl restart cu-rates-web.service
```

### Snapshot Rollback
Public snapshot publication uses atomic pointer swaps (`MASTER.md` §6):
1. To roll back to a previous snapshot, update `/var/lib/cu-rates/snapshots/pointer.json` with the previous `snapshot_id` and hash.
2. The web application dynamically re-reads the snapshot version on request without requiring a service restart.

### Emergency Withdrawal Denylist
To instantly withdraw an offer, institution, product, or snapshot:
1. Append the target ID to `/var/lib/cu-rates/snapshots/denylist.json` (`MASTER.md` §5, §6).
2. The web layer filters denied entities on the next incoming request with zero delay.

---

## 6. Restart and Reboot Checks

### Immediate Restart Verification
Run these commands to verify clean restarts and signal handling:
```bash
# 1. Graceful restart
sudo systemctl restart cu-rates-web.service

# 2. Check service status
sudo systemctl status cu-rates-web.service

# 3. Verify loopback HTTP response
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/
# Expected output: 200

# 4. Check systemd journal logs
sudo journalctl -u cu-rates-web.service -n 50 --no-pager
```

### Reboot Survival Verification
To verify that services survive a host reboot correctly:
1. **Check enabled state before reboot:**
   ```bash
   systemctl is-enabled cu-rates-web.service
   # Expected: enabled

   systemctl is-enabled cu-rates-worker.service
   # Expected: disabled or static

   systemctl is-enabled cu-rates-worker.timer
   # Expected: disabled
   ```
2. **Reboot host:**
   ```bash
   sudo reboot
   ```
3. **Verify after reboot:**
   ```bash
   systemctl is-active cu-rates-web.service
   # Expected: active

   # Confirm port binding is strictly loopback
   ss -tulpn | grep 8000
   # Expected: 127.0.0.1:8000

   # Confirm worker service was NOT triggered on boot
   systemctl is-active cu-rates-worker.service
   # Expected: inactive
   ```

---

## 7. Uninstall Runbook

To cleanly decommission the deployment without affecting other host services:

1. **Stop and Disable Services:**
   ```bash
   sudo systemctl stop cu-rates-web.service cu-rates-worker.timer cu-rates-worker.service
   sudo systemctl disable cu-rates-web.service cu-rates-worker.timer
   ```

2. **Remove Systemd Units:**
   ```bash
   sudo rm -f /etc/systemd/system/cu-rates-*.service /etc/systemd/system/cu-rates-*.timer
   sudo systemctl daemon-reload
   ```

3. **Remove Caddy Proxy Route:**
   ```bash
   sudo rm -f /etc/caddy/conf.d/cu-rates.caddyfile
   sudo systemctl reload caddy
   ```

4. **Data Retention Decision:**
   - **Retain Data (Recommended):** Preserve `/var/lib/cu-rates` for compliance and audit evidence retention (`MASTER.md` §5).
   - **Full Purge (Destructive):**
     ```bash
     # CAUTION: Permanently deletes all local snapshots and configuration
     sudo rm -rf /var/lib/cu-rates /etc/cu-rates
     sudo userdel cu-rates
     ```
