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
├── .cache/               # uv package cache directory (mode 0755)
├── snapshots/            # Public snapshot directory (read-only for web process)
│   ├── current.json      # Atomic JSON pointer to current active snapshot version
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

### Hardcoded Unit Paths & CU_RATES_ROOT Scope
> **Notice on Hardcoded Unit Paths:** The systemd unit files (`cu-rates-web.service`, `cu-rates-worker.service`, `cu-rates-worker.timer`) assume the hardcoded production root path `/var/lib/cu-rates`. The environment variable `CU_RATES_ROOT` is supported by `deploy/install.sh` solely for installer testing, custom staging, and sandbox validation; it does NOT rewrite or template the hardcoded unit paths in `/etc/systemd/system/`. Deployments intending to use an alternate root path must update the unit files accordingly.

### Architectural Isolation Principles (`MASTER.md` §6, §7)
- **Separation of Web and Worker:** The web service process (`cu-rates-web.service`) **NEVER opens or references the worker database** (`/var/lib/cu-rates/data`). The web process reads only verified public snapshots (`/var/lib/cu-rates/snapshots/`) and static application code. Inaccessible paths are enforced in systemd (`InaccessiblePaths=/var/lib/cu-rates/data /var/lib/cu-rates/artifacts /var/lib/cu-rates/backups`). `StateDirectory` is omitted from both web and worker service units so systemd does not grant broad write access to `/var/lib/cu-rates`; the worker unit explicitly restricts writes to `ReadWritePaths=/var/lib/cu-rates/data /var/lib/cu-rates/artifacts /var/lib/cu-rates/snapshots`.
- **Unprivileged Execution:** All services execute as the dedicated system user `cu-rates` (group `cu-rates`) with no interactive shell (`/usr/sbin/nologin`).
- **Hardened Sandboxing:** Systemd units enforce `ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp=true`, `NoNewPrivileges=true`, `PrivateDevices=true`, `ProtectClock=true`, `ProtectHostname=true`, `ProtectProc=invisible`, `ProcSubset=pid`, `CapabilityBoundingSet=` (empty), `AmbientCapabilities=` (empty), `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, `SystemCallArchitectures=native`, `SystemCallFilter=@system-service`, `UMask=0077`, kernel tunables protection, strict `ReadWritePaths`, and resource quotas (`MemoryMax`, `TasksMax`, `CPUQuota`). Private temporary files are protected with `PrivateTmp=true`; `/tmp` is omitted from `ReadWritePaths`.

---

## 3. Installation Runbook

The deployment bundle provides an idempotent installer `deploy/install.sh`.  
It defaults to `--dry-run` and refuses to make changes unless `--apply` is passed.

### Step 1: Preview Installation (Dry-Run)
Inspect the planned actions on an empty root before applying modifications:
```bash
sudo bash deploy/install.sh --dry-run
```

### Step 2: Apply Filesystem and Service Setup
Execute idempotent provisioning on the empty `/var/lib/cu-rates` root first to create the directory layout and systemd units:
```bash
sudo bash deploy/install.sh --apply
```
*(If `/var/lib/cu-rates` already exists and is non-empty from a previous deployment, pass `--adopt` to proceed safely without recursive chown).*

This step:
- Creates the unprivileged `cu-rates` user and group.
- Creates the layout under `/var/lib/cu-rates` (including `/var/lib/cu-rates/app` and `/var/lib/cu-rates/.cache`) with safe ownership and permissions (`cu-rates:cu-rates`), chowning only what is created; it never runs `chown -R` on existing directories.
- Copies `deploy/env.example` to `/etc/cu-rates/cu-rates.env` (mode 0600) if not present.
- Installs `cu-rates-web.service`, `cu-rates-worker.service`, and `cu-rates-worker.timer` into `/etc/systemd/system/`.
- Enables `cu-rates-web.service` on boot.
- **Explicitly ensures `cu-rates-worker.service` and `cu-rates-worker.timer` remain DISABLED per Decision D-15.**

### Step 3: Clone Repository as Service User
Clone the application repository into the pre-created `/var/lib/cu-rates/app` directory AS the dedicated `cu-rates` service user (avoiding root ownership and git dubious ownership issues):
```bash
sudo -u cu-rates git clone https://github.com/aisetsyoufree/curatesv2.git /var/lib/cu-rates/app
cd /var/lib/cu-rates/app
```

### Step 4: Provision Virtual Environment
Install production dependencies including Gunicorn into the isolated venv at `/var/lib/cu-rates/venv` as the `cu-rates` service user, setting `UV_PROJECT_ENVIRONMENT` and a writable `UV_CACHE_DIR` inside the state dir:
```bash
cd /var/lib/cu-rates/app
sudo -u cu-rates UV_PROJECT_ENVIRONMENT=/var/lib/cu-rates/venv UV_CACHE_DIR=/var/lib/cu-rates/.cache/uv uv sync --locked --no-dev --group deploy
```

### Step 5: Initialize Emergency Withdrawal Ledger
The emergency withdrawal mechanism requires an initialized ledger file at `/var/lib/cu-rates/snapshots/denylist.json` before any withdrawal records can be appended (uninitialized reads fail closed). Initial provisioning creates the parent directory `/var/lib/cu-rates/snapshots` but does not establish the ledger. Initialize the empty ledger as the `cu-rates` service user:
```bash
sudo -u cu-rates /var/lib/cu-rates/venv/bin/python -c '
from pathlib import Path
from cu_rates.snapshot import Denylist

denylist = Denylist(Path("/var/lib/cu-rates/snapshots/denylist.json"))
denylist.initialize()
'
```
This explicitly establishes the empty ledger with version 1 (`mode 0640`) so subsequent operator withdrawals can append records safely.

### Step 6: Configure Environment
Review and edit `/etc/cu-rates/cu-rates.env`:
```bash
sudo nano /etc/cu-rates/cu-rates.env
# Ensure mode 0600 is retained:
sudo chmod 0600 /etc/cu-rates/cu-rates.env
```
> **Note on Environment Variables:** The Flask application in `src/cu_rates/web/` does NOT read environment variables today (`create_app` takes an explicit configuration dictionary). Therefore, application variables in `/etc/cu-rates/cu-rates.env` (such as `CANONICAL_ORIGIN`, `ENABLE_ANALYTICS`, `CU_RATES_SNAPSHOT_DIR`) currently have no effect until environment-to-config wiring is implemented in task T055. Only `GUNICORN_CMD_ARGS` is read by Gunicorn directly.

### Step 7: Configure Caddy Reverse Proxy & Cloudflare Tunnel

1. **Host-Level Global Logging Configuration (REQUIRED):**
   `deploy/caddy/Caddyfile` is a site-only fragment. Site-level `log` blocks in Caddy govern ONLY HTTP access logs; Caddy's runtime and error logs (`http.log.error` and the default logger) are configured at the host level in the global options block. Without global logging filtering, upstream proxy failures (e.g., when the backend Gunicorn service is unavailable or returning errors) can still emit request URIs with query strings to the system journal.

   Because other services may share the Caddy installation on the Raspberry Pi 5, the deployment bundle cannot enforce host-level global configuration. **This global snippet must be merged by the operator into `/etc/caddy/Caddyfile`; the bundle cannot enforce it.**

   Ensure the global options block at the top of `/etc/caddy/Caddyfile` contains the following configuration, setting the default logger to level ERROR (or WARN), writing to stderr, and applying a `format filter` that deletes `request>uri` and `request>headers` (`request>remote_ip` is allowed to remain):
```caddy
{
    log {
        output stderr
        level ERROR
        format filter {
            wrap console
            fields {
                request>uri delete
                request>headers delete
            }
        }
    }
}
```

   The operator must verify a failing upstream request leaves no query string in `journalctl -u caddy`:
   ```bash
   # Verify error logging query redaction (trigger request while upstream or endpoint returns error):
   curl -s -i "http://127.0.0.1:8080/nonexistent?test_query_string=redact_me"
   # Inspect Caddy journal logs:
   sudo journalctl -u caddy -n 20 --no-pager
   # Confirm no query string appears in the journal log output.
   ```

2. **Integrate `deploy/caddy/Caddyfile` Site Fragment:**
   `deploy/caddy/Caddyfile` is a **site-only fragment** (no global options block) to prevent syntax conflicts with existing global configuration in `/etc/caddy/Caddyfile`. HTTP access logging is discarded (`log { output discard }`) at the Caddy layer because structured query-free access logging is handled upstream by Gunicorn in `cu-rates-web.service`.

   Copy the fragment into Caddy's configuration directory:
   ```bash
   sudo mkdir -p /etc/caddy/conf.d
   sudo cp deploy/caddy/Caddyfile /etc/caddy/conf.d/cu-rates.caddyfile
   ```

   Add the explicit `import` directive to `/etc/caddy/Caddyfile` (if not already present):
   ```caddy
   import /etc/caddy/conf.d/*.caddyfile
   ```

   Validate the Caddy configuration with `caddy validate` before reloading:
   ```bash
   caddy validate --config /etc/caddy/Caddyfile
   ```

   Reload Caddy to apply configuration:
   ```bash
   sudo systemctl reload caddy
   ```

3. **Cloudflare Tunnel Routing:**
   In Cloudflare Tunnel (`cloudflared` configuration), configure ingress to route `cu-rates.org` to Caddy at `http://127.0.0.1:8080`:
   ```yaml
   ingress:
     - hostname: cu-rates.org
       service: http://127.0.0.1:8080
     - hostname: www.cu-rates.org
       service: http://127.0.0.1:8080
     - service: http_status:404
   ```

### Step 8: Start Web Service
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
   sudo -u cu-rates UV_PROJECT_ENVIRONMENT=/var/lib/cu-rates/venv UV_CACHE_DIR=/var/lib/cu-rates/.cache/uv uv sync --locked --no-dev --group deploy
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
sudo -u cu-rates UV_PROJECT_ENVIRONMENT=/var/lib/cu-rates/venv UV_CACHE_DIR=/var/lib/cu-rates/.cache/uv uv sync --locked --no-dev --group deploy
sudo systemctl restart cu-rates-web.service
```

### Snapshot Rollback (Not yet effective)
> **STATUS: NOT WIRED / NOT YET EFFECTIVE**  
> Public snapshot rollback via atomic pointer swaps (`MASTER.md` §6) is **not yet wired** in the web application (`src/cu_rates/web` never opens `current.json`). Wiring dynamic snapshot switching requires the web app to integrate `SnapshotReader` in a later task. Until wired, updating `current.json` has no effect on served traffic without manual app reconfiguration and restart.

Target procedure once wired:
1. To roll back to a retained snapshot version, invoke `SnapshotPublisher.rollback()` (from `src/cu_rates/snapshot/builder.py`):
   ```python
   from pathlib import Path
   from cu_rates.snapshot.builder import SnapshotPublisher

   publisher = SnapshotPublisher(Path("/var/lib/cu-rates/snapshots"))
   info = publisher.rollback("<target-snapshot-id>")
   ```
   `SnapshotPublisher.rollback()` verifies the retained snapshot database and version descriptor in `/var/lib/cu-rates/snapshots/versions/<snapshot_id>` before atomically activating it by writing `/var/lib/cu-rates/snapshots/current.json`.
2. The web application dynamically re-reads the snapshot version from `current.json` on request without requiring a service restart.

### Emergency Withdrawal Denylist (Not yet effective)
> **STATUS: NOT WIRED / NOT YET EFFECTIVE**  
> Emergency withdrawal denylisting (`MASTER.md` §5, §6) is **not yet wired** in the web application (`src/cu_rates/web` never opens `denylist.json`). Dropping denied rows dynamically requires the web app to integrate `SnapshotReader` denylist filtering in a later task. Until wired, editing `denylist.json` has no effect on served traffic.

> **Prerequisite:** Withdrawal requires an initialized ledger. If `/var/lib/cu-rates/snapshots/denylist.json` was not already initialized during initial setup, run `Denylist.initialize()` once before appending entries (otherwise uninitialized reads fail closed):
> ```python
> from pathlib import Path
> from cu_rates.snapshot import Denylist
>
> denylist = Denylist(Path("/var/lib/cu-rates/snapshots/denylist.json"))
> denylist.initialize()
> ```

Target procedure once wired:
1. Append an emergency withdrawal record using `Denylist.withdraw()` (from `src/cu_rates/snapshot/denylist.py`) with all required audit fields:
   ```python
   from datetime import datetime, timezone
   from pathlib import Path
   from cu_rates.contracts.audit import WithdrawalEntry
   from cu_rates.contracts.enums import WithdrawalScope
   from cu_rates.snapshot import Denylist

   denylist = Denylist(Path("/var/lib/cu-rates/snapshots/denylist.json"))
   entry = WithdrawalEntry(
       withdrawal_id="wd-20261117-001",
       scope=WithdrawalScope.OFFER,  # "offer", "product", "institution", "source", or "snapshot"
       target_id="off-example-123",
       actor="operator@cu-rates.org",
       withdrawn_at=datetime.now(timezone.utc),
       reason="Emergency suppression due to erroneous rate claim",
   )
   denylist.withdraw(entry)
   ```
   `Denylist.withdraw()` acquires the writer lock, validates required audit fields (`withdrawal_id`, `scope`, `target_id`, `actor`, `withdrawn_at`, `reason`), and atomically writes the updated ledger to `/var/lib/cu-rates/snapshots/denylist.json`.
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
