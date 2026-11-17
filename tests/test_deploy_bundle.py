"""Tests for the Raspberry Pi 5 deployment bundle."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = REPO_ROOT / "deploy"
SYSTEMD_DIR = DEPLOY_DIR / "systemd"


def parse_systemd_unit(path: Path) -> dict[str, dict[str, list[str]]]:
    """Parse a systemd unit file into a dictionary of sections and key-value lists."""
    result: dict[str, dict[str, list[str]]] = {}
    current_section = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1]
            if current_section not in result:
                result[current_section] = {}
        elif "=" in line and current_section:
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            result[current_section].setdefault(key, []).append(val)
    return result


def test_systemd_units_exist() -> None:
    """Verify that all required systemd unit files exist."""
    assert (SYSTEMD_DIR / "cu-rates-web.service").is_file()
    assert (SYSTEMD_DIR / "cu-rates-worker.service").is_file()
    assert (SYSTEMD_DIR / "cu-rates-worker.timer").is_file()


def test_systemd_hardening_and_resource_limits() -> None:
    """Verify that every service unit contains required hardening and resource-limit keys."""
    service_files = [
        SYSTEMD_DIR / "cu-rates-web.service",
        SYSTEMD_DIR / "cu-rates-worker.service",
    ]

    required_keys = [
        "NoNewPrivileges",
        "PrivateTmp",
        "ProtectSystem",
        "ProtectHome",
        "PrivateDevices",
        "ProtectClock",
        "ProtectHostname",
        "ProtectProc",
        "ProcSubset",
        "CapabilityBoundingSet",
        "AmbientCapabilities",
        "RestrictAddressFamilies",
        "SystemCallArchitectures",
        "SystemCallFilter",
        "UMask",
        "StateDirectory",
        "LogsDirectory",
        "ReadWritePaths",
        "MemoryMax",
        "TasksMax",
        "CPUQuota",
        "Restart",
    ]

    for service_file in service_files:
        parsed = parse_systemd_unit(service_file)
        service_sec = parsed.get("Service", {})

        for key in required_keys:
            assert key in service_sec, f"{service_file.name} is missing required key '{key}'"

        # Assert hardening values
        assert service_sec["NoNewPrivileges"] == ["true"]
        assert service_sec["PrivateTmp"] == ["true"]
        assert service_sec["ProtectSystem"] == ["strict"]
        assert service_sec["ProtectHome"] == ["true"]
        assert service_sec["PrivateDevices"] == ["true"]
        assert service_sec["ProtectClock"] == ["true"]
        assert service_sec["ProtectHostname"] == ["true"]
        assert service_sec["ProtectProc"] == ["invisible"]
        assert service_sec["ProcSubset"] == ["pid"]
        assert service_sec["CapabilityBoundingSet"] == [""]
        assert service_sec["AmbientCapabilities"] == [""]
        assert service_sec["RestrictAddressFamilies"] == ["AF_UNIX AF_INET AF_INET6"]
        assert service_sec["SystemCallArchitectures"] == ["native"]
        assert service_sec["SystemCallFilter"] == ["@system-service"]
        assert service_sec["UMask"] == ["0077"]
        assert service_sec["StateDirectory"] == ["cu-rates"]
        assert service_sec["LogsDirectory"] == ["cu-rates"]
        assert service_sec["Restart"] == ["on-failure"]

        # Assert resource limits are specified
        assert any("M" in limit or "G" in limit for limit in service_sec["MemoryMax"])
        assert any(int(tasks) > 0 for tasks in service_sec["TasksMax"])
        assert any("%" in quota for quota in service_sec["CPUQuota"])


def test_loopback_only_binding() -> None:
    """Verify that web service and reverse proxy bind strictly to loopback addresses."""
    web_unit = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-web.service")
    exec_start = " ".join(web_unit.get("Service", {}).get("ExecStart", []))

    # Assert Gunicorn binds to 127.0.0.1
    assert "--bind 127.0.0.1:" in exec_start
    assert "0.0.0.0" not in exec_start

    # Assert Caddyfile binds to 127.0.0.1 and proxies to 127.0.0.1:8000
    caddyfile = (DEPLOY_DIR / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    assert "bind 127.0.0.1" in caddyfile
    assert "reverse_proxy 127.0.0.1:8000" in caddyfile
    assert "0.0.0.0" not in caddyfile


def test_no_secrets_or_real_hostnames() -> None:
    """Verify that deploy files contain no secret credentials or real private hostnames."""
    forbidden_patterns = [
        re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----"),
        re.compile(r"(?i)(password|secret|api_key|token)\s*=\s*['\"][^'\"]+['\"]"),
        re.compile(r"192\.168\.\d+\.\d+"),
        re.compile(r"10\.\d+\.\d+\.\d+"),
        re.compile(r"172\.(1[6-9]|2\d|3[01])\.\d+\.\d+"),
    ]

    for path in DEPLOY_DIR.rglob("*"):
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            for pattern in forbidden_patterns:
                rel = path.relative_to(REPO_ROOT)
                assert not pattern.search(content), (
                    f"File {rel} matched forbidden pattern {pattern.pattern}"
                )


def test_worker_unit_disabled_by_default() -> None:
    """Verify that worker service and timer are disabled by default per Decision D-15."""
    worker_service = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-worker.service")
    install_sec = worker_service.get("Install", {})
    assert "WantedBy" not in install_sec or not install_sec["WantedBy"]

    worker_timer = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-worker.timer")
    timer_install = worker_timer.get("Install", {})
    assert "WantedBy" not in timer_install or not timer_install["WantedBy"]

    # Verify install.sh does not enable the worker and disables it explicitly
    install_sh = (DEPLOY_DIR / "install.sh").read_text(encoding="utf-8")
    assert "systemctl enable cu-rates-web.service" in install_sh
    assert "systemctl enable cu-rates-worker" not in install_sh
    assert "systemctl disable cu-rates-worker.service" in install_sh
    assert "systemctl disable cu-rates-worker.timer" in install_sh


def test_web_unit_does_not_reference_worker_database() -> None:
    """Verify that the web unit reads only public snapshots and never references worker data."""
    web_unit = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-web.service")
    service_sec = web_unit.get("Service", {})

    rw_paths = " ".join(service_sec.get("ReadWritePaths", []))
    ro_paths = " ".join(service_sec.get("ReadOnlyPaths", []))
    raw_content = (SYSTEMD_DIR / "cu-rates-web.service").read_text(encoding="utf-8")

    # The web unit must never reference the operational worker data path
    assert "/var/lib/cu-rates/data" not in rw_paths
    assert "/var/lib/cu-rates/data" not in ro_paths
    assert "cu_rates.sqlite" not in raw_content
    assert "operational.db" not in raw_content
    assert "worker.db" not in raw_content

    # Web unit must only reference snapshots and app code
    assert "/var/lib/cu-rates/snapshots" in ro_paths
    assert "/var/lib/cu-rates/app" in ro_paths


def test_install_script_syntax() -> None:
    """Verify that deploy/install.sh passes bash syntax validation."""
    install_sh = DEPLOY_DIR / "install.sh"
    res = subprocess.run(["bash", "-n", str(install_sh)], capture_output=True, text=True)
    assert res.returncode == 0, f"bash -n failed: {res.stderr}"


def test_install_script_dry_run_changes_nothing(tmp_path: Path) -> None:
    """Verify that running install.sh without --apply changes nothing on disk."""
    install_sh = DEPLOY_DIR / "install.sh"
    test_root = tmp_path / "cu-rates-test-root"

    # Run without --apply pointing to a test root
    env = {**os.environ, "CU_RATES_ROOT": str(test_root)}
    res = subprocess.run(
        ["bash", str(install_sh)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0
    assert "[DRY-RUN]" in res.stdout
    assert "no changes will be made" in res.stdout
    assert "--apply" in res.stdout
    assert not test_root.exists(), "install.sh without --apply created directories on disk!"

    # Explicit --dry-run flag
    res_dry = subprocess.run(
        ["bash", str(install_sh), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_dry.returncode == 0
    assert "[DRY-RUN]" in res_dry.stdout
    assert not test_root.exists()


def test_install_script_apply_fails_loudly_on_error(tmp_path: Path) -> None:
    """Verify that install.sh --apply stops on a failing command and dry-run changes nothing."""
    install_sh = DEPLOY_DIR / "install.sh"
    test_root = tmp_path / "cu-rates-apply-fail"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()

    # Create dummy id and useradd commands that report app user exists, but failing chown
    (fake_bin / "id").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "id").chmod(0o755)

    (fake_bin / "chown").write_text("#!/bin/sh\necho 'chown error' >&2\nexit 1\n", encoding="utf-8")
    (fake_bin / "chown").chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "CU_RATES_ROOT": str(test_root),
    }

    # --apply must fail loudly when a command fails
    res_apply = subprocess.run(
        ["bash", str(install_sh), "--apply"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_apply.returncode != 0, "--apply must fail loudly on error"
    assert "chown error" in res_apply.stderr or res_apply.returncode != 0

    # Plain run (dry-run default) must succeed without error and change nothing on disk
    dry_root = tmp_path / "cu-rates-dry-noop"
    env_dry = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "CU_RATES_ROOT": str(dry_root),
    }
    res_dry = subprocess.run(
        ["bash", str(install_sh)],
        capture_output=True,
        text=True,
        env=env_dry,
    )
    assert res_dry.returncode == 0
    assert not dry_root.exists(), "Dry run must not create directories"


def test_install_script_refuses_non_empty_root(tmp_path: Path) -> None:
    """Verify that install.sh refuses non-empty root directory unless --adopt is given."""
    install_sh = DEPLOY_DIR / "install.sh"
    existing_root = tmp_path / "existing-non-empty"
    existing_root.mkdir()
    (existing_root / "marker.txt").write_text("existing data", encoding="utf-8")

    env = {**os.environ, "CU_RATES_ROOT": str(existing_root)}

    # Refuses without --adopt
    res = subprocess.run(
        ["bash", str(install_sh)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode != 0
    assert "--adopt" in res.stderr

    # Succeeds with --adopt in dry run
    res_adopt = subprocess.run(
        ["bash", str(install_sh), "--adopt"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_adopt.returncode == 0
    assert "[DRY-RUN]" in res_adopt.stdout


def test_install_script_no_hidden_failures_or_recursive_chown() -> None:
    """Verify that install.sh never does recursive chown or swallows errors with || true."""
    install_sh = (DEPLOY_DIR / "install.sh").read_text(encoding="utf-8")

    assert "chown -R" not in install_sh
    assert "|| true" not in install_sh
    assert "--adopt" in install_sh


def test_caddyfile_configuration() -> None:
    """Verify Caddy reverse proxy configuration behind Cloudflare Tunnel."""
    caddyfile = (DEPLOY_DIR / "caddy" / "Caddyfile").read_text(encoding="utf-8")

    # Plain HTTP on loopback port 8080 with auto_https off
    assert "auto_https off" in caddyfile
    assert "http://cu-rates.org:8080" in caddyfile
    assert "bind 127.0.0.1" in caddyfile
    assert "reverse_proxy 127.0.0.1:8000" in caddyfile
    assert "443" not in caddyfile

    # Forwarded client IP header (CF-Connecting-IP)
    assert "CF-Connecting-IP" in caddyfile

    # Must contain comment stating app must not trust forwarded header for security decisions
    assert "must not trust" in caddyfile.lower() or "not trust" in caddyfile.lower()

    # Must NOT duplicate app security headers from src/cu_rates/web/app.py
    duplicated_headers = [
        "Content-Security-Policy",
        "Strict-Transport-Security",
        "X-Frame-Options",
        "X-Content-Type-Options",
        "Referrer-Policy",
        "Permissions-Policy",
    ]
    for header in duplicated_headers:
        assert header not in caddyfile, f"Caddyfile duplicates app header '{header}'"

    # Keeps header removal that app does not set
    assert "-Server" in caddyfile


def test_tunnel_port_consistency() -> None:
    """Verify Cloudflare Tunnel target port is consistent between Caddyfile and README."""
    caddyfile = (DEPLOY_DIR / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")

    assert ":8080" in caddyfile
    assert "8080" in readme
    assert "http://127.0.0.1:8080" in readme


def test_web_unit_access_log_format_omits_query_string() -> None:
    """Verify that web unit access log format omits query strings (use %(U)s not %(r)s)."""
    web_unit = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-web.service")
    exec_start = " ".join(web_unit.get("Service", {}).get("ExecStart", []))

    assert "--access-logformat" in exec_start
    assert "%(U)s" in exec_start
    assert "%(r)s" not in exec_start
    assert "%(q)s" not in exec_start


def test_env_example_content_and_annotations() -> None:
    """Verify deploy/env.example explains Flask env status and marks each variable."""
    env_content = (DEPLOY_DIR / "env.example").read_text(encoding="utf-8")

    # Plain statement explaining create_app takes config dict and does not read env today
    assert (
        "create_app takes a config dict" in env_content
        or "does NOT read environment variables" in env_content
    )
    assert "T055" in env_content

    # Every active variable must be annotated
    assert "read by: gunicorn only" in env_content
    assert "not yet read by the app (T055)" in env_content
    assert "GUNICORN_CMD_ARGS=" in env_content
    assert "CANONICAL_ORIGIN=https://cu-rates.org" in env_content
    assert "CU_RATES_SNAPSHOT_DIR=" in env_content
    assert "CU_RATES_LIVE_COLLECTION_ENABLED=0" in env_content

    # Unused variables removed
    removed_vars = [
        "CU_RATES_ROOT=",
        "CU_RATES_DATA_DIR=",
        "CU_RATES_APP_DIR=",
        "CU_RATES_VENV_DIR=",
        "CU_RATES_BACKUP_DIR=",
        "CU_RATES_ARTIFACTS_DIR=",
        "CU_RATES_SCHEDULER_ENABLED=",
        "CU_RATES_MOCK_TRANSPORT_ONLY=",
        "WEB_HOST=",
        "WEB_PORT=",
        "WEB_WORKERS=",
        "WEB_THREADS=",
    ]
    for var in removed_vars:
        assert var not in env_content, f"Variable '{var}' should have been removed from env.example"


def test_deploy_readme_runbook_completeness() -> None:
    """Verify that deploy/README.md documents all required operational procedures and notices."""
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")

    required_sections = [
        "STATUS: UNEXECUTED DEPLOYMENT BUNDLE",
        "REQUIRED OWNER APPROVALS BEFORE LIVE EXECUTION",
        "Prerequisites",
        "Disk Layout",
        "Installation Runbook",
        "Upgrade Runbook",
        "Rollback Runbook",
        "Restart and Reboot Checks",
        "Uninstall Runbook",
        "D-15",
        "D-09",
    ]

    for section in required_sections:
        assert section in readme, f"README.md missing section: {section}"
