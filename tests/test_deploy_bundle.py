"""Tests for the Raspberry Pi 5 deployment bundle."""

from __future__ import annotations

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
    env = {"CU_RATES_ROOT": str(test_root)}
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


def test_env_example_placeholders_only() -> None:
    """Verify that deploy/env.example contains only placeholders, guidance, and safe flags."""
    env_content = (DEPLOY_DIR / "env.example").read_text(encoding="utf-8")

    assert "CANONICAL_ORIGIN=https://cu-rates.org" in env_content
    assert "CU_RATES_ROOT=/var/lib/cu-rates" in env_content
    assert "CU_RATES_SNAPSHOT_DIR=" in env_content
    assert "CU_RATES_DATA_DIR=" in env_content
    assert "CU_RATES_LIVE_COLLECTION_ENABLED=0" in env_content
    assert "CU_RATES_SCHEDULER_ENABLED=0" in env_content
    assert "0600" in env_content
    assert "EnvironmentFile" in env_content or "LoadCredential" in env_content


def test_caddyfile_security_headers_consistent_with_app() -> None:
    """Verify that Caddy reverse proxy security headers match Flask application headers."""
    caddyfile = (DEPLOY_DIR / "caddy" / "Caddyfile").read_text(encoding="utf-8")

    expected_headers = [
        "Content-Security-Policy",
        'X-Content-Type-Options "nosniff"',
        'X-Frame-Options "DENY"',
        'Referrer-Policy "strict-origin-when-cross-origin"',
        'Permissions-Policy "geolocation=(), camera=(), microphone=(), payment=()"',
        'Strict-Transport-Security "max-age=31536000; includeSubDomains"',
    ]

    for header in expected_headers:
        assert header in caddyfile, f"Caddyfile missing header: {header}"


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
