# CU-Rates.org V2 — Raspberry Pi 5 Deployment Runbook

> **STATUS: UNEXECUTED DEPLOYMENT BUNDLE**  
> This deployment bundle and runbook have been prepared for review and verification.  
> **Nothing has been executed on the target host.**  
>
> **REQUIRED OWNER APPROVALS BEFORE LIVE EXECUTION:**
> 1. **Product Owner Approval:** Formal review and approval of the deployment plan and target environment.
> 2. **Host Review:** Review of the existing services currently running on the Raspberry Pi 5 to confirm isolated port allocations (127.0.0.1:8080 for Caddy reverse proxy, 127.0.0.1:18200 for Gunicorn), dedicated service user (`cu-rates`), and NVMe filesystem capacity.
> 3. **Counsel / Policy Clearance:** Verification of data collection and terms boundaries per `MASTER.md` §5 before any collection enablement.
> 4. **Worker Transport Restriction (Decisions D-15, D-17):** The worker service and timer MUST remain **STOPPED, DISABLED, and MASKED**. Live collection is prohibited until the gateway pinned dialer (issue #68) is implemented, reviewed, and approved.
> 5. **Cloudflare Tunnel Routing:** Verification of the `cloudflared` tunnel ingress configuration routing `cu-rates.org` to the local Caddy reverse proxy on loopback port 8080 (`http://127.0.0.1:8080`) without inbound router ports (`MASTER.md` D-09). Caddy in turn proxies traffic to Gunicorn on `127.0.0.1:18200`.

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
- **Port choice:** Gunicorn uses `127.0.0.1:18200` because port 8000 is already used by another service on the target Pi (checked 2026-11-17); Caddy uses `127.0.0.1:8080`. Re-check `ss -ltn` for both ports before installing on a different host.
- **Host Co-existence:** Other services already run on this Pi 5. CU-Rates runs under an isolated unprivileged user (`cu-rates`) and binds strictly to loopback (`127.0.0.1:8080` for Caddy and `127.0.0.1:18200` for Gunicorn), ensuring zero port collisions and zero interference with host services.

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
│   └── versions/         # Verified, immutable public SQLite databases
├── independent/          # Operator-owned independent withdrawal/denylist ledger (root:cu-rates, mode 0755)
│   └── denylist.json     # Emergency withdrawal denylist (root:cu-rates, mode 0640)
├── data/                 # Operational SQLite database (WAL mode; worker only, mode 0750)
├── artifacts/            # Content-addressed raw evidence store (worker only, mode 0700)
├── backups/              # Encrypted backup staging area (mode 0750)
└── installed-commit      # Root-owned deployment commit record (mode 0644, D-17)

/etc/cu-rates/
└── cu-rates.env          # Externalized environment configuration (mode 0600)

/run/cu-rates-web/        # Ephemeral web runtime tmpfs directory (mode 0755)
/run/cu-rates-worker/     # Ephemeral worker runtime tmpfs directory (mode 0750)
```

### Hardcoded Unit Paths & CU_RATES_ROOT Scope
> **Notice on Hardcoded Unit Paths:** The systemd unit files (`cu-rates-web.service`, `cu-rates-worker.service`, `cu-rates-worker.timer`) assume the hardcoded production root path `/var/lib/cu-rates`. The environment variable `CU_RATES_ROOT` is supported by `deploy/install.sh` solely for installer testing, custom staging, and sandbox validation; it does NOT rewrite or template the hardcoded unit paths in `/etc/systemd/system/`. Deployments intending to use an alternate root path must update the unit files accordingly.

### Architectural Isolation Principles (`MASTER.md` §6, §7)
- **Separation of Web and Worker:** The web service process (`cu-rates-web.service`) **NEVER opens or references the worker database** (`/var/lib/cu-rates/data`). The web process reads only verified public snapshots (`/var/lib/cu-rates/snapshots/`), the independent emergency withdrawal ledger (`/var/lib/cu-rates/independent/`), and static application code. Inaccessible paths are enforced in systemd (`InaccessiblePaths=/var/lib/cu-rates/data /var/lib/cu-rates/artifacts /var/lib/cu-rates/backups`). `StateDirectory` is omitted from both web and worker service units so systemd does not grant broad write access to `/var/lib/cu-rates`; the worker unit explicitly restricts writes to `ReadWritePaths=/var/lib/cu-rates/data /var/lib/cu-rates/artifacts /var/lib/cu-rates/snapshots` and keeps `/var/lib/cu-rates/independent` strictly read-only (`ReadOnlyPaths=/var/lib/cu-rates/app /var/lib/cu-rates/independent`).
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
- Creates the layout under `/var/lib/cu-rates` (including `/var/lib/cu-rates/app`, `/var/lib/cu-rates/.cache`, and operator-owned `/var/lib/cu-rates/independent`) with safe ownership and permissions, chowning only what is created; it never runs `chown -R` on existing directories.
- Copies `deploy/env.example` to `/etc/cu-rates/cu-rates.env` (mode 0600) if not present.
- Installs `cu-rates-web.service`, `cu-rates-worker.service`, and `cu-rates-worker.timer` into `/etc/systemd/system/`.
- Enables `cu-rates-web.service` on boot.
- **Explicitly ensures `cu-rates-worker.service` and `cu-rates-worker.timer` remain STOPPED, DISABLED, and MASKED (`ln -sf /dev/null /etc/systemd/system/<unit>`) per Decisions D-15 and D-17.**
- **Records installed commit in root-owned state file `/var/lib/cu-rates/installed-commit` (mode 0644) per Decision D-17.**

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
The emergency withdrawal mechanism requires an initialized ledger file at `/var/lib/cu-rates/independent/denylist.json` before any withdrawal records can be appended (uninitialized reads fail closed). Initial provisioning creates the parent directory `/var/lib/cu-rates/independent` (operator-owned, mode 0755) but does not establish the ledger. Set its group to the web-reader group and initialize the ledger **as root**:
```bash
sudo chown root:cu-rates /var/lib/cu-rates/independent
sudo chmod 0755 /var/lib/cu-rates/independent
sudo /var/lib/cu-rates/venv/bin/python -c '
from pathlib import Path
from cu_rates.snapshot import Denylist

denylist = Denylist(Path("/var/lib/cu-rates/independent/denylist.json"))
denylist.initialize()
'
sudo chown root:cu-rates /var/lib/cu-rates/independent/denylist.json
sudo chmod 0640 /var/lib/cu-rates/independent/denylist.json
sudo -u cu-rates test -r /var/lib/cu-rates/independent/denylist.json
```
This establishes version 1 with ownership `root:cu-rates` and mode `0640`, readable by the web user without world read access. On an existing install these commands preserve the entries and repair the directory/file group and mode. Every root-run initialize, withdraw, or lift writes mode `0640` with the parent directory's group **before** atomic replacement; failure to assign the group aborts the write. Keep the parent `root:cu-rates` at `0755`: only root can create the writer lock or replace the ledger, and the worker and web services cannot modify it.

### Step 6: Configure Environment
Review and edit `/etc/cu-rates/cu-rates.env`:
```bash
sudo nano /etc/cu-rates/cu-rates.env
# Ensure mode 0600 is retained:
sudo chmod 0600 /etc/cu-rates/cu-rates.env
```
> **Environment Variables (T055):** Gunicorn's `cu_rates.web:create_app()` entry point now reads `CANONICAL_ORIGIN`, `ENABLE_ANALYTICS`, `GA_MEASUREMENT_ID`, `ADS_ENABLED`, `SNAPSHOT_ROOT`, and `DENYLIST_PATH`. `create_app(config_dict)` remains isolated from the environment for tests and embedders. Gunicorn reads `GUNICORN_CMD_ARGS` directly. The legacy `CU_RATES_SNAPSHOT_DIR` and `CU_RATES_DENYLIST_PATH` names are not read; replace them with the names above.

Unset values default to canonical origin `https://cu-rates.org`, analytics and ad slots off, no measurement ID, and no configured snapshot paths (the reader defaults to local `data/snapshots` and `data/independent/withdrawals.json`). Invalid settings abort startup. Booleans accept only `0` or `1`. `GA_MEASUREMENT_ID` must be absent/empty or match `G-[A-Z0-9]{10}` exactly. Canonical origins must be bare HTTPS DNS origins (optional valid port, optional trailing slash), without credentials, paths, queries, or fragments. Paths must be absolute without whitespace, control characters, or `..`; configured files are still verified by `SnapshotReader` on every read. Empty origin/path/boolean values are rejected. No environment setting enables live collection.

Analytics requires both `ENABLE_ANALYTICS=1` and a valid ID. All initial requests are same-origin; only an explicit saved opt-in permits loading Google Analytics. A secure, HttpOnly, SameSite=Strict preference cookie is written only after a choice and lasts 180 days. Native forms work without JavaScript; HTTPS POSTs must supply the canonical `Origin` (foreign/missing Origin is rejected). Withdrawal redirects to a page that does not load the tag and clears this site's GA cookies. Responses with consent state are private/no-store and vary on Cookie; the proxy/CDN must honor these headers. The same-origin script sets Consent Mode v2 defaults denied before any tag call. Advertising consent remains denied. CSP adds only `www.googletagmanager.com` for scripts and `www.google-analytics.com` / `region1.google-analytics.com` for connections when analytics is enabled and configured. No wildcard, inline execution, or advertising host is allowed. Google calls are not made by automated tests.

`ADS_ENABLED=1` exposes only a labeled, empty readiness placeholder with reserved dimensions in the designated footer region, after all content, assumptions, coverage and educational disclosures. Only comparison pages with displayed offers and reviewed educational articles qualify. Error, draft, thin and empty pages cannot render it. There are no creatives, scripts, publisher IDs, `ads.txt`, paid placements, or ad inputs to sorting. Templates must never include slots inside or between offer tables or above disclosures. Keep it `0` at launch; serving real ads requires a later product decision.


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

   The operator must verify a failing upstream request leaves no query string in `journalctl -u caddy` (verified after reloading Caddy in item 2 below).

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

   **Verify Redaction Probe (Controlled Upstream Failure):**
   After `systemctl reload caddy`, with the web service stopped (a controlled upstream failure), run `curl -sS -H 'Host: cu-rates.org' 'http://127.0.0.1:8080/?probe=SECRET_QUERY_VALUE'`, then check `journalctl -u caddy --since '1 minute ago'` shows a proxy error event (positive check that an error was logged) AND does NOT contain SECRET_QUERY_VALUE; restart the web service afterwards.

   ```bash
   # 1. Ensure the web service is stopped for a controlled upstream failure:
   sudo systemctl stop cu-rates-web.service

   # 2. Run redaction probe with Host header and secret query value:
   curl -sS -H 'Host: cu-rates.org' 'http://127.0.0.1:8080/?probe=SECRET_QUERY_VALUE'

   # 3. Check Caddy journal logs from the last minute:
   #    - Positive check: shows a proxy error event (e.g. dial tcp 127.0.0.1:18200: connect: connection refused)
   #    - Negative check: does NOT contain SECRET_QUERY_VALUE
   sudo journalctl -u caddy --since '1 minute ago'

   # 4. Restart the web service afterwards:
   sudo systemctl restart cu-rates-web.service
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
   curl -s -i http://127.0.0.1:18200/ | head -n 20
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

### Snapshot Rollback
The web application (`src/cu_rates/web`) uses `SnapshotReader` when `SNAPSHOT_ROOT` and `DENYLIST_PATH` are configured. It verifies `current.json` and enforces expiry and denylisting on every comparison read. Missing or invalid inputs show no offers. Pointer swaps take effect on the next read without restart.

Procedure:
1. To roll back to a retained snapshot version, invoke `SnapshotPublisher.rollback()` (from `src/cu_rates/snapshot/builder.py`):
   ```python
   from pathlib import Path
   from cu_rates.snapshot.builder import SnapshotPublisher

   publisher = SnapshotPublisher(Path("/var/lib/cu-rates/snapshots"))
   info = publisher.rollback("<target-snapshot-id>")
   ```
   `SnapshotPublisher.rollback()` verifies the retained snapshot database and version descriptor in `/var/lib/cu-rates/snapshots/versions/<snapshot_id>` before atomically activating it by writing `/var/lib/cu-rates/snapshots/current.json`.
2. The web application dynamically re-reads the snapshot version from `current.json` on request without requiring a service restart.

### Worker wiring check (issue #115)

Nothing starts the worker, and `cu-rates-worker.service` remains the disabled stub. `python -m cu_rates.worker_config check` (as the service user, with the settings from `env.example` in the environment) is a **read-only** check that every path the worker will need exists and is sane: the operational database opens, the terms ledger and both denylists parse, the state and artifact directories are private, and the operator and automatic ledgers are separate files in separate directories. It creates nothing and prints JSON; exit status 1 means something is missing or invalid. The terms ledger is written only by the operator (`TermsLedger.record`), never by the worker.

### Automatic suppression ledger (issue #118)

The independent denylist above is **operator-owned**: the collection worker can only read it. When a source blocks or challenges the (currently masked) worker, the worker records the suppression in a **separate ledger it owns**, `/var/lib/cu-rates/automatic/denylist.json` (`AUTOMATIC_DENYLIST_PATH`). The public site enforces **both** ledgers, and once `AUTOMATIC_DENYLIST_PATH` is set a missing or invalid automatic ledger blocks public reads (fail closed), so create it once, as the service user, before setting the variable:

```bash
sudo -u cu-rates /var/lib/cu-rates/venv/bin/python -c "from pathlib import Path; from cu_rates.snapshot import Denylist; Denylist(Path('/var/lib/cu-rates/automatic/denylist.json')).initialize()"
```

Only an operator lifts an automatic suppression, **as the service user**, preserving the worker's ownership of its separate ledger:

```bash
sudo -u cu-rates /var/lib/cu-rates/venv/bin/python -c "from datetime import UTC, datetime; from pathlib import Path; from cu_rates.snapshot import Denylist; Denylist(Path('/var/lib/cu-rates/automatic/denylist.json')).lift('<withdrawal id>', at=datetime.now(UTC), actor='<your name>')"
```

The health check reads both ledgers and requires group-read permission and a file group matching the parent directory's group, even when invoked by root. This is a mode/group check, not an ACL or arbitrary-user impersonation check; provision the shared group as `cu-rates`. The deployed health service also runs as `cu-rates`, checking actual read access.

**Upgrading an existing install.** The installer never overwrites `/etc/cu-rates/cu-rates.env`, so add `AUTOMATIC_DENYLIST_PATH=/var/lib/cu-rates/automatic/denylist.json` to it yourself (after creating the ledger as above, and after re-running the installer so the directory and units exist) and restart `cu-rates-web`. The web service refuses to start if `DENYLIST_PATH` is set without it, rather than silently ignoring the worker's blocks.

### Emergency Withdrawal Denylist
The web application enforces the configured `DENYLIST_PATH` through `SnapshotReader` on each read. An uninitialized, missing, or invalid `denylist.json` fails closed and suppresses offers.

> **Prerequisite:** Complete Step 5, including the `root:cu-rates` parent group and `0640` ledger permissions. If the ledger was not initialized, run `Denylist.initialize()` once **as root** before appending entries (otherwise uninitialized reads fail closed):
> ```python
> from pathlib import Path
> from cu_rates.snapshot import Denylist
>
> denylist = Denylist(Path("/var/lib/cu-rates/independent/denylist.json"))
> denylist.initialize()
> ```

Procedure:
1. Run the following Python as root with `sudo /var/lib/cu-rates/venv/bin/python`, and append an emergency withdrawal record using `Denylist.withdraw()` (from `src/cu_rates/snapshot/denylist.py`) with all required audit fields. Root is required to create the writer lock in the operator-owned directory; the replacement ledger remains group-readable at `0640`:
   ```python
   from datetime import datetime, timezone
   from pathlib import Path
   from cu_rates.contracts.audit import WithdrawalEntry
   from cu_rates.contracts.enums import WithdrawalScope
   from cu_rates.snapshot import Denylist

   denylist = Denylist(Path("/var/lib/cu-rates/independent/denylist.json"))
   entry = WithdrawalEntry(
       schema_version="1.0",
       withdrawal_id="wd-20261117-001",
       scope=WithdrawalScope.OFFER,  # "offer", "product", "institution", "source", or "snapshot"
       target_id="off-example-123",
       actor="operator@cu-rates.org",
       withdrawn_at=datetime.now(timezone.utc),
       reason="Emergency suppression due to erroneous rate claim",
   )
   denylist.withdraw(entry)
   ```
   `Denylist.withdraw()` acquires the writer lock, validates required audit fields (`schema_version`, `withdrawal_id`, `scope`, `target_id`, `actor`, `withdrawn_at`, `reason`), and atomically writes the updated ledger to `/var/lib/cu-rates/independent/denylist.json`.
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
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18200/
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
   # Expected: masked (or disabled)

   systemctl is-enabled cu-rates-worker.timer
   # Expected: masked (or disabled)
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
   ss -tulpn | grep 18200
   # Expected: 127.0.0.1:18200

   # Confirm worker service was NOT triggered on boot
   systemctl is-active cu-rates-worker.service
   # Expected: inactive
   ```

---

## 7. Scheduled Operations

CU-Rates provides automated backup and health monitoring via systemd timers. These timers are installed by `deploy/install.sh` in a disabled state, allowing operators to review configuration before enabling them.

### What Each Timer Does

1. **Daily Backup (`cu-rates-backup.timer` -> `cu-rates-backup.service`):**
   - **Schedule:** Daily at 03:17 local time with up to 30 minutes randomized delay (`RandomizedDelaySec=1800`), catch-up enabled (`Persistent=true`).
   - **Actions:** Runs `python -m cu_rates.ops.backup create` to create an atomic, integrity-verified backup of the operational SQLite database (`/var/lib/cu-rates/data/operational.sqlite`) and artifact store (`/var/lib/cu-rates/artifacts`) into `/var/lib/cu-rates/backups/backup-<timestamp>`. Immediately follows with `python -m cu_rates.ops.backup prune --destination /var/lib/cu-rates/backups --keep 14` to retain the 14 newest valid backups. Any backup failing verification is left in place for operator investigation (the CLI exits non-zero so the unit shows as failed). The data directory is writable for this unit only so SQLite can create its WAL `-shm` file for a read-only open; the tool never writes the database. **The operational database path above is a convention: no deployed worker creates it yet (issue #115), so enabling these timers before it exists makes the units fail, which is the intended alert.**
   - **Hardening & Isolation:** Unprivileged execution as `cu-rates`, `ProtectSystem=strict`, no network access (`PrivateNetwork=true`, `RestrictAddressFamilies=AF_UNIX`), `UMask=0077`, `Type=oneshot`, and writes ONLY to `/var/lib/cu-rates/backups` (`ReadWritePaths`). Non-zero exit marks the unit as failed without swallowing errors.

2. **Periodic Health Check (`cu-rates-health.timer` -> `cu-rates-health.service`):**
   - **Schedule:** Every 15 minutes (`OnCalendar=*:00/15:00`) with catch-up enabled (`Persistent=true`).
   - **Actions:** Runs `python -m cu_rates.ops.health` to inspect:
     - Public snapshot readability and offer freshness (`/var/lib/cu-rates/snapshots`)
     - Independent denylist / withdrawal ledger readability (`/var/lib/cu-rates/independent/denylist.json`)
     - Newest backup validity and age (`/var/lib/cu-rates/backups`)
     - Operational SQLite database integrity via `PRAGMA quick_check` (`/var/lib/cu-rates/data/operational.sqlite`)
   - **Hardening & Isolation:** Strictly read-only everywhere (`ReadOnlyPaths`, no `ReadWritePaths`), unprivileged execution as `cu-rates`, no network access (`PrivateNetwork=true`, `RestrictAddressFamilies=AF_UNIX`), `Type=oneshot`, `Restart=no`.
   - **Alert Hook:** Non-zero exit (exit code 1) on any check failure marks `cu-rates-health.service` as failed in systemd.

3. **Daily Data Quality Check (`cu-rates-quality.timer` -> `cu-rates-quality.service`):**
   - **Schedule:** Daily at 06:11 local time with up to 15 minutes randomized delay (`RandomizedDelaySec=900`), catch-up enabled (`Persistent=true`).
   - **Actions:** Runs `python -m cu_rates.ops.quality --database /var/lib/cu-rates/data/operational.sqlite` to report on data quality across the operational database:
     - Quarantined offers: warns if older than 24 hours, fails if older than 7 days
     - Stale sources: warns if an approved source has had no successful run in >72 hours, fails if >7 days or never run
     - Blocked sources: warns if 1–3 sources blocked in the last 7 days, fails if >3
     - Open withdrawals: reports open withdrawals in force
     - Unresolved corrections: warns on corrections without subsequent review decisions
   - **Hardening & Isolation:** Unprivileged execution as `cu-rates`, `ProtectSystem=strict`, no network access (`PrivateNetwork=true`, `RestrictAddressFamilies=AF_UNIX`), `UMask=0077`, `Type=oneshot`, and writes ONLY to the data directory (`ReadWritePaths=/var/lib/cu-rates/data`) for SQLite WAL side files (`-shm`/`-wal`) during read-only opens.
   - **Alert Hook:** Non-zero exit (exit status 1) on any failing check marks `cu-rates-quality.service` as failed in systemd; warnings print to JSON but exit 0. Alert delivery is not implemented yet (failures record to the system journal).

> **Status Notice:**
> - **Off-device encrypted copies and alert delivery are NOT implemented yet.** Backups currently stage locally on NVMe under `/var/lib/cu-rates/backups` and require an operator-managed key before remote export is implemented. Automated alert delivery (e.g. webhook, email, or paging notification on unit failure) has not yet been wired; failures record to the system journal.

### Enabling Scheduled Timers

To enable and immediately start the timers:
```bash
sudo systemctl enable --now cu-rates-backup.timer cu-rates-health.timer cu-rates-quality.timer
```

### Checking Timer Status and Logs

Inspect active timer schedules:
```bash
systemctl list-timers
systemctl status cu-rates-backup.timer cu-rates-health.timer cu-rates-quality.timer
```

Check execution output and error logs:
```bash
# Health check logs
journalctl -u cu-rates-health

# Backup service logs
journalctl -u cu-rates-backup

# Quality check logs
journalctl -u cu-rates-quality
```

---

## 7b. Correction Inbox (issue #100)

The public site has a correction form (`/corrections`). It is **closed until you set `INTAKE_DB`**, so nothing is stored until someone has agreed to read the inbox.

**What it is.** A small private SQLite file (`/var/lib/cu-rates/intake/intake.sqlite`, directory mode 0700, owned by `cu-rates`) separate from the worker database, artifacts and snapshots. The web process can only **add** cases to it. Cases and their history are append-only; the optional reply email lives in a separate table, is never copied into the history, and is deleted 90 days after a case is resolved.

**To open it** (after a person is assigned to the inbox):

```bash
sudo -u cu-rates /var/lib/cu-rates/venv/bin/python -m cu_rates.intake --db /var/lib/cu-rates/intake/intake.sqlite init
# set INTAKE_DB=/var/lib/cu-rates/intake/intake.sqlite in /etc/cu-rates/cu-rates.env, then:
sudo systemctl restart cu-rates-web
sudo systemctl enable --now cu-rates-intake-purge.timer   # daily deletion of expired reply contacts
```

**Working the inbox** (as the service user): `... --db <path> list` (exit status 3 means a case missed its triage target: one business day for rate, identity, privacy and security reports, five for others), `show <id>`, `triage`, `note`, `resolve --decision confirmed|amended|quarantined|rejected`, `bulk-reject --from ... --to ... --yes` (junk flooding), and `redact` (removes unsolicited personal data from a case). The purge timer runs `purge-contacts`.

**Withdraw a case as root**, after completing Step 5. This immediately suppresses the target on the independent denylist, then records the decision in the existing intake database. The service user cannot create the ledger's writer lock in the root-owned directory. Use the actual case ID, target, actor and reason:

```bash
sudo /var/lib/cu-rates/venv/bin/python -m cu_rates.intake --db /var/lib/cu-rates/intake/intake.sqlite withdraw '<case-id>' --denylist /var/lib/cu-rates/independent/denylist.json --scope offer --target '<offer-id>' --actor '<your name>' --note '<reason>'
sudo -u cu-rates test -r /var/lib/cu-rates/independent/denylist.json
```

The root write retains mode `0640` and the parent directory's `cu-rates` group; it never grants world read access. Lifts on the independent ledger also run as root, with the same Python pattern, `Denylist(Path('/var/lib/cu-rates/independent/denylist.json')).lift('<withdrawal id>', at=datetime.now(UTC), actor='<your name>')`. Never lift the worker's automatic ledger as root: a root-created lock file would block the worker. A group-assignment error aborts replacement and must be resolved before retrying; missing, unreadable or invalid ledgers continue to fail closed.

**Limits to know.** The form's rate limits are per web process (three Gunicorn workers), so roughly 6 reports per client and 45 in total per hour per host; one determined client can still fill the inbox, which is what `bulk-reject` is for. The client address used for those limits comes from `CF-Connecting-IP` and is trusted for that purpose only.

## 8. Uninstall Runbook

To cleanly decommission the deployment without affecting other host services:

1. **Stop and Disable Services:**
   ```bash
   sudo systemctl stop cu-rates-web.service cu-rates-worker.timer cu-rates-worker.service cu-rates-backup.timer cu-rates-backup.service cu-rates-health.timer cu-rates-health.service cu-rates-quality.timer cu-rates-quality.service
   sudo systemctl disable cu-rates-web.service cu-rates-worker.timer cu-rates-backup.timer cu-rates-health.timer cu-rates-quality.timer
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
