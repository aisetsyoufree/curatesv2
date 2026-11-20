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
├── app/                  # Application code repository clone (root:cu-rates, mode 0755)
├── venv/                 # uv-managed Python 3.12 virtual environment (root:cu-rates, mode 0755, deploy group)
├── .cache/               # uv package cache directory (mode 0755)
├── snapshots/            # Public snapshot directory (read-only for web process)
│   ├── current.json      # Atomic JSON pointer to current active snapshot version
│   └── versions/         # Verified, immutable public SQLite databases
├── independent/          # Operator-owned independent withdrawal/denylist ledger (root:cu-rates, mode 0755)
│   └── denylist.json     # Emergency withdrawal denylist (root:cu-rates, mode 0640)
├── automatic/            # Worker-owned automatic suppression ledger (cu-rates:cu-rates)
│   └── denylist.json     # Automatic suppression denylist (cu-rates:cu-rates)
├── intake/               # Correction inbox directory (cu-rates:cu-rates, mode 0700)
│   └── intake.sqlite     # Private correction inbox database
├── data/                 # Operational SQLite database (WAL mode; worker only, mode 0750)
├── artifacts/            # Content-addressed raw evidence store (worker only, mode 0700)
├── backups/              # Local private backup staging (tool enforces mode 0700)
└── installed-commit      # Root-owned deployment commit record (mode 0644, D-17)

/etc/cu-rates/
└── cu-rates.env          # Externalized environment configuration (root:cu-rates, mode 0640)

/run/cu-rates-web/        # Ephemeral web runtime tmpfs directory (mode 0755)
/run/cu-rates-worker/     # Ephemeral worker runtime tmpfs directory (mode 0750)
```

### Hardcoded Unit Paths & CU_RATES_ROOT Scope
> **Notice on Hardcoded Unit Paths:** The systemd unit files (`cu-rates-web.service`, `cu-rates-worker.service`, `cu-rates-worker.timer`) assume the hardcoded production root path `/var/lib/cu-rates`. The environment variable `CU_RATES_ROOT` is supported by `deploy/install.sh` solely for installer testing, custom staging, and sandbox validation; it does NOT rewrite or template the hardcoded unit paths in `/etc/systemd/system/`. Deployments intending to use an alternate root path must update the unit files accordingly.

### Architectural Isolation Principles (`MASTER.md` §6, §7)
- **Separation of Web and Worker:** The web service process (`cu-rates-web.service`) **NEVER opens or references the worker database** (`/var/lib/cu-rates/data`). The web process reads only verified public snapshots (`/var/lib/cu-rates/snapshots/`), the independent emergency withdrawal ledger (`/var/lib/cu-rates/independent/`), the automatic suppression ledger (`/var/lib/cu-rates/automatic/`), and static application code. Inaccessible paths are enforced in systemd (`InaccessiblePaths=/var/lib/cu-rates/data /var/lib/cu-rates/artifacts /var/lib/cu-rates/backups`). `StateDirectory` is omitted from both web and worker service units so systemd does not grant broad write access to `/var/lib/cu-rates`; the worker unit explicitly restricts writes to `ReadWritePaths=/var/lib/cu-rates/data /var/lib/cu-rates/artifacts /var/lib/cu-rates/snapshots /var/lib/cu-rates/automatic` and keeps `/var/lib/cu-rates/independent` strictly read-only (`ReadOnlyPaths=/var/lib/cu-rates/app /var/lib/cu-rates/independent`).
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
- Copies `deploy/env.example` to `/etc/cu-rates/cu-rates.env` (root:cu-rates, mode 0640) if not present.
- Installs all 11 systemd units from `deploy/systemd/*` (`cu-rates-web.service`, `cu-rates-worker.service`, `cu-rates-worker.timer`, `cu-rates-backup.service`, `cu-rates-backup.timer`, `cu-rates-health.service`, `cu-rates-health.timer`, `cu-rates-quality.service`, `cu-rates-quality.timer`, `cu-rates-intake-purge.service`, and `cu-rates-intake-purge.timer`) into `/etc/systemd/system/`.
- Enables `cu-rates-web.service` on boot.
- **Explicitly ensures `cu-rates-worker.service` and `cu-rates-worker.timer` remain STOPPED, DISABLED, and MASKED (`ln -sf /dev/null /etc/systemd/system/<unit>`) per Decisions D-15 and D-17.**
- **Records installed commit in root-owned state file `/var/lib/cu-rates/installed-commit` (mode 0644) per Decision D-17.**

### Step 3: Clone Repository as Root at a Pinned Commit
The code the service runs must not be writable by the service user (it would otherwise let a compromised web process alter code that root later executes for withdrawals and backups). `app/` and `venv/` are therefore `root:cu-rates` (readable, not writable, by the service). Clone as root and check out an explicit reviewed tag or full commit SHA, then record what actually runs (D-17):
```bash
sudo git clone https://github.com/aisetsyoufree/curatesv2.git /var/lib/cu-rates/app
sudo git -C /var/lib/cu-rates/app checkout --detach <reviewed-tag-or-sha>
sudo git -C /var/lib/cu-rates/app rev-parse HEAD | sudo tee /var/lib/cu-rates/installed-commit
```
The installer's own `installed-commit` records the bundle it ran from; the command above replaces it with the commit that is actually deployed.

### Step 4: Provision Virtual Environment as Root
Build the venv as root so the service user cannot modify code or dependencies, using the lockfile only:
```bash
cd /var/lib/cu-rates/app
sudo UV_PROJECT_ENVIRONMENT=/var/lib/cu-rates/venv uv sync --locked --no-dev --group deploy
sudo chmod -R go-w /var/lib/cu-rates/app /var/lib/cu-rates/venv
sudo -u cu-rates /var/lib/cu-rates/venv/bin/python -c "import cu_rates"   # must be readable by the service user
```
Updating code later is the same procedure as root; restart `cu-rates-web` afterwards. The environment file `/etc/cu-rates/cu-rates.env` is `root:cu-rates` mode 0640.

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
# Ensure root:cu-rates ownership and mode 0640 are retained:
sudo chown root:cu-rates /etc/cu-rates/cu-rates.env
sudo chmod 0640 /etc/cu-rates/cu-rates.env
```
> **Environment Variables (T055):** Gunicorn's `cu_rates.web:create_app()` entry point now reads `CANONICAL_ORIGIN`, `ENABLE_ANALYTICS`, `GA_MEASUREMENT_ID`, `ADS_ENABLED`, `SNAPSHOT_ROOT`, `DENYLIST_PATH`, `AUTOMATIC_DENYLIST_PATH`, and `INTAKE_DB`. `create_app(config_dict)` remains isolated from the environment for tests and embedders. Gunicorn reads `GUNICORN_CMD_ARGS` directly. Optional worker paths for the read-only configuration check (`python -m cu_rates.worker_config check`) include `OPERATIONAL_DB`, `TERMS_LEDGER_PATH`, `WORKER_STATE_DIR`, and `ARTIFACT_ROOT`. The legacy `CU_RATES_SNAPSHOT_DIR` and `CU_RATES_DENYLIST_PATH` names are not read; replace them with the names above.

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
   sudo git fetch --tags
   sudo git checkout --detach <target-tag-or-sha>
   git rev-parse HEAD | sudo tee /var/lib/cu-rates/installed-commit
   ```

2. **Update Virtual Environment:**
   ```bash
   sudo UV_PROJECT_ENVIRONMENT=/var/lib/cu-rates/venv uv sync --locked --no-dev --group deploy
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
sudo git checkout --detach <previous-tag-or-sha>
sudo UV_PROJECT_ENVIRONMENT=/var/lib/cu-rates/venv uv sync --locked --no-dev --group deploy
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

Nothing starts the worker, and `cu-rates-worker.service` remains the disabled stub.
The operator-led pilot in MASTER.md D-23 does not change these deployment
restrictions: no model scheduler, live worker, or import of real rate batches is
enabled by this runbook. `python -m cu_rates.worker_config check` (as the service user,
with the settings from `env.example` in the environment) is a **read-only** check that
every path the worker will need exists and is sane: the operational database opens,
the terms ledger and both denylists parse, the state and artifact directories are
private, and the operator and automatic ledgers are separate files in separate
directories. It creates nothing and prints JSON; exit status 1 means something is
missing or invalid. The terms ledger is written only by the operator
(`TermsLedger.record`), never by the worker.

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
   - **Actions:** Runs `python -m cu_rates.ops.backup create` to create an atomic, integrity-verified backup of the operational SQLite database (`/var/lib/cu-rates/data/operational.sqlite`), artifact store (`/var/lib/cu-rates/artifacts`), operator ledger (`/var/lib/cu-rates/independent/denylist.json`), automatic ledger (`/var/lib/cu-rates/automatic/denylist.json`), private correction intake (`/var/lib/cu-rates/intake/intake.sqlite`), and snapshot pointer plus committed versions (`/var/lib/cu-rates/snapshots`) into `/var/lib/cu-rates/backups/backup-<timestamp>`. Immediately follows with `python -m cu_rates.ops.backup prune --destination /var/lib/cu-rates/backups --keep 14` to retain the 14 newest valid backups. Any backup failing verification is left in place for operator investigation (the CLI exits non-zero so the unit shows as failed). Both SQLite files use the online backup API, including committed WAL data. The data and intake directories are writable only for SQLite WAL side files during read-only opens; the tool never writes either source database. **The operational database path above is a convention: no deployed worker creates it yet (issue #115), so enabling these timers before it exists makes the units fail, which is the intended alert.**
   - **Hardening & Isolation:** Unprivileged execution as `cu-rates`, `ProtectSystem=strict`, no network access (`PrivateNetwork=true`, `RestrictAddressFamilies=AF_UNIX`), `UMask=0077`, `Type=oneshot`, and `ReadWritePaths` limited to backups, data and intake (the latter two for WAL side files). Artifacts, both ledgers, snapshots and application code remain read-only. The intake directory stays mode 0700, owned by `cu-rates`; backed-up/restored intake and ledgers are 0600, all backup/restore directories 0700, and restored snapshot versions remain sealed read-only. Non-zero exit marks the unit as failed without swallowing errors.

2. **Periodic Health Check (`cu-rates-health.timer` -> `cu-rates-health.service`):**
   - **Schedule:** Every 15 minutes (`OnCalendar=*:00/15:00`) with catch-up enabled (`Persistent=true`).
   - **Actions:** Runs `python -m cu_rates.ops.health` to inspect:
     - Public snapshot readability and offer freshness (`/var/lib/cu-rates/snapshots`)
     - Independent denylist / withdrawal ledger readability (`/var/lib/cu-rates/independent/denylist.json`)
     - Newest backup validity and age (`/var/lib/cu-rates/backups`)
     - Operational SQLite database integrity via `PRAGMA quick_check` (`/var/lib/cu-rates/data/operational.sqlite`)
   - **Hardening & Isolation:** Unprivileged execution as `cu-rates`, `ProtectSystem=strict`, no network access (`PrivateNetwork=true`, `RestrictAddressFamilies=AF_UNIX`), `UMask=0077`, `Type=oneshot`, `Restart=no`, and writes ONLY to the data directory (`ReadWritePaths=/var/lib/cu-rates/data`) for SQLite WAL side files (`-shm`/`-wal`) during read-only opens.
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

### Backup scope and offline restore

The create options `--denylist-path`, `--automatic-denylist-path`, `--intake-database`,
and `--snapshot-root` are optional for compatibility with older invocations. The service
passes all four paths above. A supplied input that is missing, invalid, symlinked or
unreadable fails the backup; it is never silently omitted. Initialize both ledgers and
intake using the earlier runbook steps, and publish a verified snapshot before enabling
this service. Do not re-initialize empty ledgers after a disk loss.

Backups with optional state use manifest version 2; legacy database/artifact-only backups
remain version 1. `verify` checks hashes and sizes for every file, SQLite integrity for
both databases, valid withdrawal history, and snapshot descriptors/databases. `prune`
uses those checks and preserves invalid backups for investigation. Private reply contacts
are present in intake backups: keep them access-controlled and never print or log their
contents. The existing contact purge does not retroactively remove contacts from backups;
apply the retention policy to backup copies as well. This task adds no encryption or
off-device copying. Terms and worker state outside the operational database remain
outside this bounded backup scope.

Snapshots are copied as `current.json` and all committed `versions/<id>/` releases.
Publisher locks and unfinished `.build-*` directories are excluded. The tool copies
immutable versions first and refuses a pointer change during copying (retry the backup);
it validates the copied pointer and every committed version. Database, intake and ledgers
are individually consistent copies, not a single transaction across stores. On recovery,
the publication gate and both ledgers must still be enforced; a backup grants no new
publication approval and does not extend snapshot expiry.

Stop the web, worker, publisher, intake purge and backup services/timers before restore.
Restore first to a private staging directory on a trusted local filesystem:

```bash
/var/lib/cu-rates/venv/bin/python -m cu_rates.ops.backup verify /absolute/backup-<timestamp>
/var/lib/cu-rates/venv/bin/python -m cu_rates.ops.backup restore /absolute/backup-<timestamp> /absolute/recovery
```

The result contains the operational database by its original filename, `artifacts/`,
`independent/denylist.json`, `automatic/denylist.json`, `intake/intake.sqlite`, and
`snapshots/`. Missing optional inputs in a legacy backup remain absent. The target must
be absent or empty by default. To protect withdrawal history during recovery, first
place any surviving ledgers in the target's `independent/` and `automatic/` directories.
`--replace-ledgers` permits a target containing **only** these ledger files and their
directories (including writer lock files), and refuses missing backed-up replacements or newer/different destination
history (including lifts and disjoint IDs). Only a deliberate owner-authorized rollback
may add `--allow-ledger-rollback` alongside `--replace-ledgers`; this can re-expose offers.
Other existing files, symlinks and backup/target overlap are always refused. Keep all
writers stopped during the check and replacement. A staging restore cannot inspect
surviving ledgers elsewhere: never manually install an older ledger over a live one.

After verification, install recovered components into the deployment paths while services
remain stopped. Restore their deployed ownership and directory access before restarting:
the operator ledger directory is `root:cu-rates`, 0750, ledger 0640; the automatic ledger
must be readable by the web user, and the intake directory/file stay `cu-rates`, 0700/0600.
The recovery bundle's stricter 0700/0600 permissions are intentional and must not be
relaxed to world-readable access. Verify web health and both ledgers before resuming reads.

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

**Backup unit symptom.** If `cu-rates-backup.service` fails with `status=226/NAMESPACE`, one of its read-only state directories (independent, automatic, snapshots, intake) does not exist yet; create it (see Step 2) rather than loosening the unit. Backups copy every snapshot version ever published, so watch disk growth.

**Working the inbox** (as the service user): `... --db <path> list` (exit status 3 means a case missed its triage target: one business day for rate, identity, privacy and security reports, five for others), `show <id>`, `triage`, `note`, `resolve --decision confirmed|amended|quarantined|rejected`, `bulk-reject --from ... --to ... --yes` (junk flooding), and `redact` (removes unsolicited personal data from a case). The purge timer runs `purge-contacts`.

**Withdraw a case as root**, after completing Step 5. This immediately suppresses the target on the independent denylist, then records the decision in the existing intake database. The service user cannot create the ledger's writer lock in the root-owned directory. Use the actual case ID, target, actor and reason:

```bash
sudo /var/lib/cu-rates/venv/bin/python -m cu_rates.intake --db /var/lib/cu-rates/intake/intake.sqlite withdraw '<case-id>' --denylist /var/lib/cu-rates/independent/denylist.json --scope offer --target '<offer-id>' --actor '<your name>' --note '<reason>'
sudo -u cu-rates test -r /var/lib/cu-rates/independent/denylist.json
```

The root write retains mode `0640` and the parent directory's `cu-rates` group; it never grants world read access. Lifts on the independent ledger also run as root, with the same Python pattern, `Denylist(Path('/var/lib/cu-rates/independent/denylist.json')).lift('<withdrawal id>', at=datetime.now(UTC), actor='<your name>')`. Never lift the worker's automatic ledger as root: a root-created lock file would block the worker. A group-assignment error aborts replacement and must be resolved before retrying; missing, unreadable or invalid ledgers continue to fail closed.

**Limits to know.** The form's rate limits are per web process (three Gunicorn workers), so roughly 6 reports per client and 45 in total per hour per host; one determined client can still fill the inbox, which is what `bulk-reject` is for. The client address used for those limits comes from `CF-Connecting-IP` and is trusted for that purpose only.

## 7c. Private Operator Review Console

SSH to the Pi, then run the console as the installation's service user (`cu-rates`
by default; use the `APP_USER` chosen for a customized install). Pass the actual
worker `OPERATIONAL_DB` as `--db`, and the public site's `INTAKE_DB` as
`--intake-db` if correction intake is configured. These are explicit absolute paths;
the console does not load `/etc/cu-rates/cu-rates.env` or guess a database. Keep
both databases and their parent directories private to the service user (0700
directories, 0600 files); allow SQLite WAL side files in those directories.

After installing the reviewed code/venv, stop operational writers, take a verified
backup, and migrate the operational DB to Alembic head from the installed checkout:

```bash
cd /var/lib/cu-rates/app
sudo -u cu-rates /var/lib/cu-rates/venv/bin/python -c "from pathlib import Path; from cu_rates.db import upgrade_database; upgrade_database(Path('/var/lib/cu-rates/data/operational.sqlite'))"
sudo -u cu-rates /var/lib/cu-rates/venv/bin/python -m cu_rates.review --db /var/lib/cu-rates/data/operational.sqlite --intake-db /var/lib/cu-rates/intake/intake.sqlite list
sudo -u cu-rates /var/lib/cu-rates/venv/bin/python -m cu_rates.review --db /var/lib/cu-rates/data/operational.sqlite show product '<product-id>'
sudo -u cu-rates /var/lib/cu-rates/venv/bin/python -m cu_rates.review --db /var/lib/cu-rates/data/operational.sqlite approve product '<product-id>' --sha256 '<sha256 from show>' --actor '<your name>' --note '<evidence and rationale>'
```

Omit `--intake-db` until intake exists. Use `show source '<request-id>'` and
`approve source` or `reject source` with the same required flags for source requests;
`reject product` records a content-bound rejection. Review the full evidence excerpts
and the exact approval scope printed by `show`. Use its top-level `sha256` token,
not `reviewed_product_sha256`. Changed content, stale source approvals and repeated
decisions are refused. A rejection is final for that unchanged product/request;
changed products require another review. Product age is the earliest stored offer
observation, or explicitly unknown when there is no stored timestamp.

Source uncertainty is routed by acquisition code using
`cu_rates.review.store.enqueue_source_review(conn, proposal, charter=..., profile_id=...,
reason=..., at=...)` in its transaction. It takes a validated `SourceApproval` template,
discards its proposed verdict/actor/time/id/rationale, and appends an idempotent
`source_review_requests` row with the proposed policy, profile/layout/parser/origins,
evidence excerpts and a digest of the current source binding. **Enqueueing grants no
approval.** No production uncertainty router exists yet; callers must use this helper
when that routing is implemented. The CLI alone writes the owner's decision via the
existing `insert_approval` path, using the request ID as the approval ID. A stale
request remains visible: route a new proposal to approve current content; explicitly
rejecting the old request records a source rejection and blocks publication. It does
not merely dismiss a notification. Requests and decisions cannot be updated, deleted
or replaced, and downgrade refuses to discard a populated queue.

Quarantined offers show persisted validation/rejection reasons and a manual `rerun`
guidance marker; the console cannot release quarantine or execute an adapter. If no
reason was persisted it says so; confidence tiers are not yet persisted by production
adapters. Corrections are read-only here and point to `python -m cu_rates.intake`
`show`, `triage`, `resolve` and `withdraw`; reply contacts are never read or displayed.
Approval does not publish a snapshot, bypass identity/robots/terms controls or lift a
denylist entry. The collector's disabled/masked state remains governed by D-15/D-18.

For the optional read-only page, keep this process running in an SSH session:

```bash
sudo -u cu-rates /var/lib/cu-rates/venv/bin/python -m cu_rates.review --db /var/lib/cu-rates/data/operational.sqlite --intake-db /var/lib/cu-rates/intake/intake.sqlite serve --port 8765
# On your own computer, in another terminal:
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 '<ssh-host>'
```

Open `http://127.0.0.1:8765/` locally. The server only accepts a `127.0.0.1` bind,
uses no JavaScript or external resources, and offers no approval actions. All decisions
stay in the CLI with an actor, note and hash. Stop it with Ctrl-C. It is for trusted
host users and SSH tunnel users, with no separate web login. Never add this port or
application to Caddy, Cloudflare Tunnel, a public service, or the public Flask app.
The installer needs no additional service or public route.

## 8. Uninstall Runbook

To cleanly decommission the deployment without affecting other host services:

1. **Stop and Disable Services:**
   ```bash
   sudo systemctl stop cu-rates-web.service cu-rates-worker.timer cu-rates-worker.service cu-rates-backup.timer cu-rates-backup.service cu-rates-health.timer cu-rates-health.service cu-rates-quality.timer cu-rates-quality.service cu-rates-intake-purge.timer cu-rates-intake-purge.service
   sudo systemctl disable cu-rates-web.service cu-rates-worker.timer cu-rates-backup.timer cu-rates-health.timer cu-rates-quality.timer cu-rates-intake-purge.timer
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
