"""Tests for the Raspberry Pi 5 deployment bundle."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

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
    assert (SYSTEMD_DIR / "cu-rates-backup.service").is_file()
    assert (SYSTEMD_DIR / "cu-rates-backup.timer").is_file()
    assert (SYSTEMD_DIR / "cu-rates-health.service").is_file()
    assert (SYSTEMD_DIR / "cu-rates-health.timer").is_file()
    assert (SYSTEMD_DIR / "cu-rates-quality.service").is_file()
    assert (SYSTEMD_DIR / "cu-rates-quality.timer").is_file()


def test_systemd_hardening_and_resource_limits() -> None:
    """Verify that every service unit contains required hardening and resource-limit keys."""
    service_files = [
        SYSTEMD_DIR / "cu-rates-web.service",
        SYSTEMD_DIR / "cu-rates-worker.service",
    ]

    common_required_keys = [
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

        for key in common_required_keys:
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
        assert service_sec["LogsDirectory"] == ["cu-rates"]

        # Assert resource limits are specified
        assert any("M" in limit or "G" in limit for limit in service_sec["MemoryMax"])
        assert any(int(tasks) > 0 for tasks in service_sec["TasksMax"])
        assert any("%" in quota for quota in service_sec["CPUQuota"])

    # Service-specific checks
    web_parsed = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-web.service")
    web_sec = web_parsed.get("Service", {})
    assert "StateDirectory" not in web_sec, "cu-rates-web.service must not define StateDirectory"
    assert web_sec["Restart"] == ["on-failure"]

    worker_parsed = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-worker.service")
    worker_sec = worker_parsed.get("Service", {})
    assert "StateDirectory" not in worker_sec, (
        "cu-rates-worker.service must not define StateDirectory"
    )
    assert worker_sec["Restart"] == ["no"], "cu-rates-worker.service must have Restart=no"
    worker_rw = set(" ".join(worker_sec.get("ReadWritePaths", [])).split())
    assert worker_rw == {
        "/var/lib/cu-rates/data",
        "/var/lib/cu-rates/artifacts",
        "/var/lib/cu-rates/snapshots",
        "/var/lib/cu-rates/automatic",  # its OWN suppression ledger (issue #118)
    }, f"worker ReadWritePaths changed unexpectedly: {worker_rw}"
    worker_ro = set(" ".join(worker_sec.get("ReadOnlyPaths", [])).split())
    assert "/var/lib/cu-rates/independent" in worker_ro
    assert "/var/lib/cu-rates/independent" not in worker_rw


def test_loopback_only_binding() -> None:
    """Verify that web service and reverse proxy bind strictly to loopback addresses."""
    web_unit = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-web.service")
    exec_start = " ".join(web_unit.get("Service", {}).get("ExecStart", []))

    # Assert Gunicorn binds to 127.0.0.1
    assert "--bind 127.0.0.1:" in exec_start
    assert "0.0.0.0" not in exec_start

    # Assert Caddyfile binds to 127.0.0.1 and proxies to 127.0.0.1:18200
    caddyfile = (DEPLOY_DIR / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    assert "bind 127.0.0.1" in caddyfile
    assert "reverse_proxy 127.0.0.1:18200" in caddyfile
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

    # Verify install.sh does not enable the worker and disables, stops, and masks it
    install_sh = (DEPLOY_DIR / "install.sh").read_text(encoding="utf-8")
    assert "systemctl enable cu-rates-web.service" in install_sh
    assert "systemctl enable cu-rates-worker" not in install_sh
    assert "systemctl disable cu-rates-worker.service" in install_sh
    assert "systemctl disable cu-rates-worker.timer" in install_sh
    assert "systemctl stop cu-rates-worker.service" in install_sh
    assert "systemctl stop cu-rates-worker.timer" in install_sh
    assert "ln -sf /dev/null" in install_sh


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

    # Web unit must only reference snapshots, independent ledger, and app code
    assert "/var/lib/cu-rates/snapshots" in ro_paths
    assert "/var/lib/cu-rates/independent" in ro_paths
    assert "/var/lib/cu-rates/app" in ro_paths


def test_web_unit_cannot_write_app_data_and_inaccessible_paths() -> None:
    """Verify that web unit cannot write app data: no StateDirectory, strict InaccessiblePaths."""
    web_unit = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-web.service")
    service_sec = web_unit.get("Service", {})

    # StateDirectory must be absent to prevent systemd from creating/granting write access
    assert "StateDirectory" not in service_sec, (
        "StateDirectory must be absent from web unit so it cannot write application data"
    )

    # InaccessiblePaths must block data, artifacts, and backup directories
    inaccessible_entries = service_sec.get("InaccessiblePaths", [])
    inaccessible_paths = [p for entry in inaccessible_entries for p in entry.split()]

    required_inaccessible = [
        "/var/lib/cu-rates/data",
        "/var/lib/cu-rates/artifacts",
        "/var/lib/cu-rates/backups",
    ]
    for req in required_inaccessible:
        assert req in inaccessible_paths, (
            f"cu-rates-web.service must have {req} in InaccessiblePaths"
        )

    # ReadWritePaths must strictly contain only ephemeral runtime dirs
    rw_entries = service_sec.get("ReadWritePaths", [])
    rw_paths = [p for entry in rw_entries for p in entry.split()]
    for req in required_inaccessible:
        assert req not in rw_paths, f"ReadWritePaths must not include {req}"
    assert "/var/lib/cu-rates" not in rw_paths


def test_execstart_binary_paths_match_readme_venv() -> None:
    """Verify that ExecStart binaries in all units reside under the venv path in README."""
    readme_content = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")

    # Extract documented venv environment path from README
    match = re.search(r"UV_PROJECT_ENVIRONMENT=([^\s]+)", readme_content)
    assert match is not None, "README.md must document UV_PROJECT_ENVIRONMENT"
    documented_venv = match.group(1)
    assert documented_venv == "/var/lib/cu-rates/venv"
    assert (
        "UV_PROJECT_ENVIRONMENT=/var/lib/cu-rates/venv"
        " UV_CACHE_DIR=/var/lib/cu-rates/.cache/uv uv sync --locked --no-dev --group deploy"
        in readme_content
    )

    # Verify each systemd service unit's ExecStart binary is under documented venv
    service_files = [
        SYSTEMD_DIR / "cu-rates-web.service",
        SYSTEMD_DIR / "cu-rates-worker.service",
        SYSTEMD_DIR / "cu-rates-backup.service",
        SYSTEMD_DIR / "cu-rates-health.service",
        SYSTEMD_DIR / "cu-rates-quality.service",
    ]
    for sf in service_files:
        unit = parse_systemd_unit(sf)
        exec_start_lines = unit.get("Service", {}).get("ExecStart", [])
        assert exec_start_lines, f"{sf.name} is missing ExecStart"
        for line in exec_start_lines:
            binary = line.split()[0]
            expected_prefix = f"{documented_venv}/bin/"
            assert binary.startswith(expected_prefix), (
                f"{sf.name} binary '{binary}' is not under documented venv '{expected_prefix}'"
            )


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

    # --apply must fail loudly when a command fails with exact exit code 1 and error output
    res_apply = subprocess.run(
        ["bash", str(install_sh), "--apply"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_apply.returncode == 1, (
        f"--apply must fail with exit code 1 on command error, got {res_apply.returncode}"
    )
    assert "chown error" in res_apply.stderr

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


def test_documented_runbook_order_against_install_checks(tmp_path: Path) -> None:
    """Verify documented runbook order and that install.sh creates app/ owned by cu-rates."""
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")

    # Documented order assertions:
    # 1. install.sh --apply runs on empty root
    # 2. git clone runs as cu-rates into pre-created app/
    # 3. uv sync runs as cu-rates with UV_CACHE_DIR and UV_PROJECT_ENVIRONMENT
    apply_step = "sudo bash deploy/install.sh --apply"
    clone_step = (
        "sudo -u cu-rates git clone https://github.com/aisetsyoufree/curatesv2.git"
        " /var/lib/cu-rates/app"
    )
    sync_step = (
        "sudo -u cu-rates UV_PROJECT_ENVIRONMENT=/var/lib/cu-rates/venv"
        " UV_CACHE_DIR=/var/lib/cu-rates/.cache/uv uv sync --locked --no-dev --group deploy"
    )

    assert apply_step in readme, "README must document install.sh --apply step"
    assert clone_step in readme, "README must document git clone as cu-rates user into app/"
    assert sync_step in readme, "README must document uv sync with UV_CACHE_DIR"

    apply_idx = readme.index(apply_step)
    clone_idx = readme.index(clone_step)
    sync_idx = readme.index(sync_step)
    assert apply_idx < clone_idx < sync_idx, (
        f"Documented order must be install.sh ({apply_idx})"
        f" -> clone ({clone_idx}) -> uv sync ({sync_idx})"
    )

    install_sh = DEPLOY_DIR / "install.sh"
    test_root = tmp_path / "empty-root"

    # Assert install.sh --dry-run plans creating app/ and setting ownership to cu-rates:cu-rates
    res_dry = subprocess.run(
        ["bash", str(install_sh), "--dry-run"],
        capture_output=True,
        text=True,
        env={**os.environ, "CU_RATES_ROOT": str(test_root)},
    )
    assert res_dry.returncode == 0
    assert f"Would create directory '{test_root}/app'" in res_dry.stdout
    assert (
        f"Would set ownership of created directory '{test_root}/app' to cu-rates:cu-rates"
        in res_dry.stdout
    )

    # Assert install.sh --apply executes mkdir and chown cu-rates:cu-rates on app/ using fake-PATH
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log_file = tmp_path / "actions.log"

    (fake_bin / "id").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "id").chmod(0o755)

    (fake_bin / "useradd").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "useradd").chmod(0o755)

    (fake_bin / "systemctl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "systemctl").chmod(0o755)

    (fake_bin / "install").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "install").chmod(0o755)

    (fake_bin / "chmod").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "chmod").chmod(0o755)

    (fake_bin / "ln").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "ln").chmod(0o755)

    mkdir_script = """#!/bin/sh
case "$*" in
    *"/etc"*|*"/run"*) exit 0 ;;
    *) exec /bin/mkdir "$@" ;;
esac
"""
    (fake_bin / "mkdir").write_text(mkdir_script, encoding="utf-8")
    (fake_bin / "mkdir").chmod(0o755)

    chown_script = f"""#!/bin/sh
echo "chown $@" >> "{log_file}"
exit 0
"""
    (fake_bin / "chown").write_text(chown_script, encoding="utf-8")
    (fake_bin / "chown").chmod(0o755)

    env_apply = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "CU_RATES_ROOT": str(test_root),
    }

    res_apply = subprocess.run(
        ["bash", str(install_sh), "--apply"],
        capture_output=True,
        text=True,
        env=env_apply,
    )
    assert res_apply.returncode == 0, f"install.sh --apply failed: {res_apply.stderr}"
    assert log_file.is_file(), "Fake chown was never invoked"
    chown_logs = log_file.read_text(encoding="utf-8")
    assert f"chown cu-rates:cu-rates {test_root}/app" in chown_logs, (
        f"install.sh --apply did not chown app/ to cu-rates:cu-rates. Logs:\n{chown_logs}"
    )


def test_install_script_no_hidden_failures_or_recursive_chown() -> None:
    """Verify that install.sh never does recursive chown or swallows errors with || true."""
    install_sh = (DEPLOY_DIR / "install.sh").read_text(encoding="utf-8")

    assert "chown -R" not in install_sh
    assert "|| true" not in install_sh
    assert "--adopt" in install_sh


VALID_PARENTS: dict[str, frozenset[str]] = {
    # Site-level directives (allowed parent: "site")
    "bind": frozenset({"site"}),
    "reverse_proxy": frozenset({"site"}),
    "header": frozenset({"site"}),
    "redir": frozenset({"site"}),
    "handle": frozenset({"site"}),
    "respond": frozenset({"site"}),
    "encode": frozenset({"site"}),
    "import": frozenset({"root", "site"}),
    # reverse_proxy subdirectives (allowed parent: "reverse_proxy")
    "header_up": frozenset({"reverse_proxy"}),
    "header_down": frozenset({"reverse_proxy"}),
    # header subdirectives (allowed parent: "header")
    "-Server": frozenset({"header"}),
    # log directive may appear at site level or within global options block
    "log": frozenset({"site", "global"}),
    # log subdirectives (allowed parent: "log")
    "output": frozenset({"log"}),
    "level": frozenset({"log"}),
    "format": frozenset({"log"}),
    # format subdirectives (allowed parent: "format")
    "wrap": frozenset({"format"}),
    "fields": frozenset({"format"}),
    # fields subdirectives (allowed parent: "fields")
    "request>uri": frozenset({"fields"}),
    "request>headers": frozenset({"fields"}),
}

ALLOWED_CADDY_DIRECTIVES = frozenset(VALID_PARENTS.keys())


def parse_and_validate_caddyfile(content: str) -> list[dict[str, object]]:
    """Parse Caddyfile structurally into a block tree, enforcing context-aware placement."""
    root_node: dict[str, object] = {
        "type": "root",
        "name": "root",
        "args": [],
        "children": [],
        "line": 0,
    }
    stack: list[dict[str, object]] = [root_node]

    for line_no, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # Strip inline comment respecting quotes
        if "#" in line:
            in_quote = False
            quote_char = ""
            for idx, ch in enumerate(line):
                if ch in ('"', "'"):
                    if not in_quote:
                        in_quote = True
                        quote_char = ch
                    elif quote_char == ch:
                        in_quote = False
                elif ch == "#" and not in_quote:
                    line = line[:idx].strip()
                    break

        if not line:
            continue

        if line == "}":
            if len(stack) <= 1:
                raise ValueError(f"Unbalanced braces: unexpected closing brace at line {line_no}")
            stack.pop()
            continue

        opens_block = False
        if line.endswith("{"):
            prefix = line[:-1].rstrip()
            opens_block = True
            tokens = shlex.split(prefix) if prefix else []
        else:
            tokens = shlex.split(line)

        current_parent_node = stack[-1]
        current_context = str(current_parent_node.get("name", "root"))
        children_list = current_parent_node.setdefault("children", [])
        assert isinstance(children_list, list)

        if current_context == "root":
            if opens_block:
                if not tokens:
                    # Global options block: "{"
                    global_node: dict[str, object] = {
                        "type": "global",
                        "name": "global",
                        "args": [],
                        "parent": "root",
                        "children": [],
                        "line": line_no,
                    }
                    children_list.append(global_node)
                    stack.append(global_node)
                else:
                    directive = tokens[0]
                    if directive in VALID_PARENTS:
                        if "root" not in VALID_PARENTS[directive]:
                            raise ValueError(
                                f"Directive '{directive}' cannot appear in context"
                                f" '{current_context}' at line {line_no}"
                            )
                        block_node: dict[str, object] = {
                            "type": "directive",
                            "name": directive,
                            "args": tokens[1:],
                            "parent": "root",
                            "children": [],
                            "line": line_no,
                        }
                        children_list.append(block_node)
                        stack.append(block_node)
                    else:
                        # Site address block (e.g. "http://cu-rates.org:8080 {")
                        site_node: dict[str, object] = {
                            "type": "site",
                            "name": "site",
                            "addresses": tokens,
                            "args": tokens,
                            "parent": "root",
                            "children": [],
                            "line": line_no,
                        }
                        children_list.append(site_node)
                        stack.append(site_node)
            else:
                if not tokens:
                    continue
                directive = tokens[0]
                if directive not in VALID_PARENTS:
                    raise ValueError(
                        f"Disallowed or unknown Caddy directive '{directive}' at line {line_no}"
                    )
                if "root" not in VALID_PARENTS[directive]:
                    raise ValueError(
                        f"Directive '{directive}' cannot appear in context"
                        f" '{current_context}' at line {line_no}"
                    )
                leaf_node: dict[str, object] = {
                    "type": "directive",
                    "name": directive,
                    "args": tokens[1:],
                    "parent": "root",
                    "children": [],
                    "line": line_no,
                }
                children_list.append(leaf_node)
        else:
            if not tokens:
                raise ValueError(f"Empty statement with opening brace at line {line_no}")
            directive = tokens[0]
            if directive not in VALID_PARENTS:
                raise ValueError(
                    f"Disallowed or unknown Caddy directive '{directive}' at line {line_no}"
                )
            if current_context not in VALID_PARENTS[directive]:
                raise ValueError(
                    f"Directive '{directive}' cannot appear in context"
                    f" '{current_context}' at line {line_no}"
                )
            node: dict[str, object] = {
                "type": "directive",
                "name": directive,
                "args": tokens[1:],
                "parent": current_context,
                "children": [],
                "line": line_no,
            }
            children_list.append(node)
            if opens_block:
                stack.append(node)

    if len(stack) != 1:
        raise ValueError(f"Unbalanced braces: {len(stack) - 1} unclosed block(s) at end of file")

    children = root_node.get("children", [])
    assert isinstance(children, list)
    return [c for c in children if isinstance(c, dict)]


def test_caddyfile_configuration() -> None:
    """Verify Caddy reverse proxy configuration behind Cloudflare Tunnel."""
    caddyfile = (DEPLOY_DIR / "caddy" / "Caddyfile").read_text(encoding="utf-8")

    # Structural parsing: must have balanced braces and only allowed directives in valid contexts
    parsed = parse_and_validate_caddyfile(caddyfile)
    assert parsed, "Caddyfile must contain structural directives"
    assert len(parsed) == 2, "Caddyfile must contain exactly two site blocks"

    canonical_site = parsed[0]
    assert canonical_site["type"] == "site"
    addresses = canonical_site.get("addresses", [])
    assert isinstance(addresses, list)
    assert "http://cu-rates.org:8080" in addresses
    canonical_children = canonical_site.get("children", [])
    assert isinstance(canonical_children, list)
    child_names = [child["name"] for child in canonical_children if isinstance(child, dict)]
    assert "bind" in child_names
    assert "reverse_proxy" in child_names
    assert "header" in child_names
    assert "log" in child_names

    # Verify reverse_proxy subdirectives
    rp_node = next(
        child
        for child in canonical_children
        if isinstance(child, dict) and child.get("name") == "reverse_proxy"
    )
    rp_children = rp_node.get("children", [])
    assert isinstance(rp_children, list)
    assert any(
        isinstance(c, dict)
        and c.get("name") == "header_up"
        and isinstance(c.get("args"), list)
        and "CF-Connecting-IP" in c["args"]
        for c in rp_children
    )

    # Site-only fragment: no global options block to avoid conflicts
    # with the host's existing main Caddyfile.
    non_comment_lines = [
        line.strip()
        for line in caddyfile.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert non_comment_lines, "Caddyfile must not be empty"
    assert not non_comment_lines[0].startswith("{"), (
        "Caddyfile must be a site-only fragment without a global options block"
    )
    assert "auto_https" not in caddyfile, "Fragment must not contain global auto_https directive"

    # Plain HTTP on loopback port 8080
    assert "http://cu-rates.org:8080" in caddyfile
    assert "bind 127.0.0.1" in caddyfile
    assert "reverse_proxy 127.0.0.1:18200" in caddyfile
    assert "443" not in caddyfile

    # Forwarded client IP header (CF-Connecting-IP)
    assert "CF-Connecting-IP" in caddyfile
    # Must NOT rewrite or forward client-IP as X-Forwarded-For
    assert "X-Forwarded-For" not in caddyfile

    # Must contain comment stating app must not trust forwarded header for security decisions
    assert "must not trust" in caddyfile.lower() or "not trust" in caddyfile.lower()

    # Access logs discarded at site level; runtime/error logging is governed globally
    assert "output discard" in caddyfile
    assert "cu-rates-access.log" not in caddyfile
    # Site fragment must not configure error logging filter (governed globally in host Caddyfile)
    assert "format filter" not in caddyfile

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


def test_caddyfile_negative_control_site_level_header_up() -> None:
    """Negative control: verify context-aware parser rejects site-level header_up."""
    caddyfile = (DEPLOY_DIR / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    corrupted = caddyfile.replace("bind 127.0.0.1", "bind 127.0.0.1\n    header_up Host {host}")
    with pytest.raises(ValueError, match="Directive 'header_up' cannot appear in context 'site'"):
        parse_and_validate_caddyfile(corrupted)


def test_caddyfile_negative_control_unknown_directive() -> None:
    """Negative control: verify structural parser rejects unknown/injected Caddy directives."""
    caddyfile = (DEPLOY_DIR / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    corrupted = caddyfile.replace(
        "bind 127.0.0.1", "bind 127.0.0.1\n    injected_bogus_directive 12345"
    )
    with pytest.raises(
        ValueError, match="Disallowed or unknown Caddy directive 'injected_bogus_directive'"
    ):
        parse_and_validate_caddyfile(corrupted)


def test_caddyfile_negative_control_unbalanced_brace() -> None:
    """Negative control: parser rejects unbalanced braces (dropped closing brace)."""
    caddyfile = (DEPLOY_DIR / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    corrupted = caddyfile.rstrip()
    assert corrupted.endswith("}")
    corrupted = corrupted[:-1].rstrip()
    with pytest.raises(ValueError, match="Unbalanced braces"):
        parse_and_validate_caddyfile(corrupted)


def test_caddy_readme_documents_required_global_logging_snippet() -> None:
    """Verify README documents the required global logging snippet for the host Caddyfile."""
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")
    exact_snippet = (
        "{\n"
        "    log {\n"
        "        output stderr\n"
        "        level ERROR\n"
        "        format filter {\n"
        "            wrap console\n"
        "            fields {\n"
        "                request>uri delete\n"
        "                request>headers delete\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "}"
    )
    assert exact_snippet in readme
    assert "journalctl -u caddy" in readme
    assert "this global snippet must be merged by the operator" in readme.lower()
    assert "the bundle cannot enforce it" in readme.lower()
    assert "failing upstream request leaves no query string" in readme.lower()

    # Verify global snippet parses into a valid context-aware block tree
    parsed_global = parse_and_validate_caddyfile(exact_snippet)
    assert parsed_global, "Global logging snippet must parse into a valid tree"
    assert parsed_global[0]["type"] == "global"
    global_children = parsed_global[0].get("children", [])
    assert isinstance(global_children, list)
    assert len(global_children) == 1
    first_child = global_children[0]
    assert isinstance(first_child, dict)
    assert first_child.get("name") == "log"


def test_caddy_readme_documents_import_and_validate_steps() -> None:
    """Verify README documents explicit Caddy import directive and validation step."""
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")
    # Exact documented import directive line is strictly required
    assert "import /etc/caddy/conf.d/*.caddyfile" in readme
    assert "caddy validate" in readme
    assert "systemctl reload caddy" in readme
    assert "conf.d" in readme


def test_caddy_readme_negative_control_remove_import() -> None:
    """Negative control: verify removing exact import line fails even if generic 'import' exists."""
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")
    corrupted = readme.replace(
        "import /etc/caddy/conf.d/*.caddyfile",
        "# import omitted for test",
    )
    with pytest.raises(AssertionError):
        assert "import /etc/caddy/conf.d/*.caddyfile" in corrupted


def test_caddy_readme_redaction_probe_verification() -> None:
    """Verify README documents redaction probe with required verification elements.

    Asserts exact elements: Host header, reload before probe, stopped upstream
    (controlled upstream failure), positive error check, negative secret check,
    and restart afterwards.
    """
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")

    # 1. Reload before probe
    reload_idx = readme.find("systemctl reload caddy")
    probe_cmd = "curl -sS -H 'Host: cu-rates.org' 'http://127.0.0.1:8080/?probe=SECRET_QUERY_VALUE'"
    probe_idx = readme.find(probe_cmd)
    assert reload_idx != -1, "README must document 'systemctl reload caddy'"
    assert probe_idx != -1, f"README must contain exact probe command: {probe_cmd}"
    assert reload_idx < probe_idx, (
        "Caddy reload ('systemctl reload caddy') must occur before redaction probe"
    )

    # 2. Host header in probe
    assert "-H 'Host: cu-rates.org'" in readme

    # 3. Stopped upstream (controlled upstream failure)
    assert "web service stopped" in readme.lower()
    assert "controlled upstream failure" in readme.lower()
    assert "stop cu-rates-web.service" in readme
    prose_stop_idx = readme.find("with the web service stopped")
    assert prose_stop_idx != -1
    assert prose_stop_idx < probe_idx, "Prose must state web service is stopped before probe"

    stop_cmd_idx = readme.find("sudo systemctl stop cu-rates-web.service")
    assert stop_cmd_idx != -1
    probe_code_idx = readme.find(probe_cmd, stop_cmd_idx)
    assert probe_code_idx != -1, "Probe command must appear after stopping web service in runbook"

    # 4. Positive error check that an error was logged
    assert "journalctl -u caddy --since '1 minute ago'" in readme
    assert "proxy error event" in readme.lower()
    assert "positive check" in readme.lower()

    # 5. Negative secret check (does not contain SECRET_QUERY_VALUE)
    assert "SECRET_QUERY_VALUE" in readme
    assert (
        "does not contain secret_query_value" in readme.lower()
        or "not contain secret_query_value" in readme.lower()
    )
    assert "negative check" in readme.lower()

    # 6. Restart the web service afterwards
    restart_idx = readme.find("restart cu-rates-web.service")
    assert restart_idx != -1, "README must document restarting web service afterwards"
    assert probe_idx < restart_idx, "Restarting web service must occur after redaction probe"


def test_caddy_readme_negative_control_probe_elements() -> None:
    """Negative control: verify probe check fails if Host header or secret checks are corrupted."""
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")

    # Host header omitted
    corrupted_no_host = readme.replace("-H 'Host: cu-rates.org' ", "")
    with pytest.raises(AssertionError):
        assert (
            "curl -sS -H 'Host: cu-rates.org' 'http://127.0.0.1:8080/?probe=SECRET_QUERY_VALUE'"
            in corrupted_no_host
        )

    # Secret check omitted
    corrupted_no_secret = readme.replace("SECRET_QUERY_VALUE", "OTHER_VALUE")
    with pytest.raises(AssertionError):
        assert "SECRET_QUERY_VALUE" in corrupted_no_secret


def test_readme_documents_denylist_initialize_step() -> None:
    """Verify README documents explicit Denylist.initialize() step in initial setup and runbook."""
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")
    assert "Denylist.initialize()" in readme
    assert "denylist.initialize()" in readme
    assert "/var/lib/cu-rates/independent/denylist.json" in readme
    assert "Denylist(Path(" in readme


def test_withdraw_runbook_uses_root_and_private_reader_group() -> None:
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")
    provisioning = readme.split("### Step 5:", 1)[1].split("### Step 6:", 1)[0]
    assert "sudo chown root:cu-rates /var/lib/cu-rates/independent" in provisioning
    assert "sudo chmod 0640 /var/lib/cu-rates/independent/denylist.json" in provisioning
    assert "0644" not in provisioning
    inbox = readme.split("## 7b.", 1)[1].split("## 8.", 1)[0]
    assert (
        "sudo /var/lib/cu-rates/venv/bin/python -m cu_rates.intake "
        "--db /var/lib/cu-rates/intake/intake.sqlite withdraw"
    ) in inbox
    assert "root" in inbox and "0640" in inbox and "cu-rates" in inbox


def test_tunnel_port_consistency() -> None:
    """Verify Cloudflare Tunnel target port is consistent between Caddyfile and README."""
    caddyfile = (DEPLOY_DIR / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")

    assert ":8080" in caddyfile
    assert "8080" in readme
    assert "http://127.0.0.1:8080" in readme


def test_web_unit_access_log_format_escaped_and_omits_query_and_referer() -> None:
    """Verify web access log format escapes %% specifiers and omits query and referer."""
    web_unit = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-web.service")
    exec_start = " ".join(web_unit.get("Service", {}).get("ExecStart", []))

    assert "--access-logformat" in exec_start

    # Systemd percent-sign escaping: all access-logformat specifiers must be doubled (%%)
    doubled_specifiers = [
        "%%(h)s",
        "%%(l)s",
        "%%(u)s",
        "%%(t)s",
        "%%(m)s",
        "%%(U)s",
        "%%(H)s",
        "%%(s)s",
        "%%(b)s",
        "%%(a)s",
    ]
    for spec in doubled_specifiers:
        assert spec in exec_start, f"ExecStart missing escaped specifier '{spec}'"

    # Confirm there are NO unescaped single percent signs followed by ( or specifier letters
    unescaped = re.findall(r"(?<!%)%[a-zA-Z(]", exec_start)
    assert not unescaped, f"ExecStart contains unescaped systemd specifiers: {unescaped}"

    # Query string must be omitted (use %%(U)s, not %%(r)s or %%(q)s)
    assert "%(r)s" not in exec_start
    assert "%%(r)s" not in exec_start
    assert "%(q)s" not in exec_start
    assert "%%(q)s" not in exec_start

    # Referer must be dropped completely
    assert "%(f)s" not in exec_start
    assert "%%(f)s" not in exec_start


def test_worker_unit_restart_is_no() -> None:
    """Verify cu-rates-worker.service has Restart=no so failing oneshot does not retry."""
    worker_unit = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-worker.service")
    service_sec = worker_unit.get("Service", {})
    assert service_sec.get("Restart") == ["no"]
    assert "RestartSec" not in service_sec


def test_env_example_content_and_annotations() -> None:
    """Verify deploy/env.example explains Flask env status and marks each variable."""
    env_content = (DEPLOY_DIR / "env.example").read_text(encoding="utf-8")

    assert "create_app() reads" in env_content
    assert "create_app(config_dict)" in env_content
    assert "Invalid values abort startup" in env_content
    assert "T055" in env_content
    assert "read by: gunicorn only" in env_content
    assert "read by: create_app()" in env_content
    for name in (
        "CANONICAL_ORIGIN",
        "ENABLE_ANALYTICS",
        "GA_MEASUREMENT_ID",
        "ADS_ENABLED",
        "SNAPSHOT_ROOT",
        "DENYLIST_PATH",
        "GUNICORN_CMD_ARGS",
    ):
        assert f"{name}=" in env_content
    assert "CANONICAL_ORIGIN=https://cu-rates.org" in env_content
    assert "ENABLE_ANALYTICS=0" in env_content
    assert "ADS_ENABLED=0" in env_content
    assert "CU_RATES_SNAPSHOT_DIR=" not in env_content
    assert "CU_RATES_DENYLIST_PATH=" not in env_content
    assert "NO EFFECT" not in env_content
    assert "does NOT read environment" not in env_content

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


def test_rollback_and_denylist_disclosed_as_configured_read_time_controls() -> None:
    """README describes the configured SnapshotReader and its fail-closed inputs."""
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")

    assert "Snapshot Rollback" in readme
    assert "not yet wired" not in readme
    assert "now reads" in readme
    assert "SNAPSHOT_ROOT" in readme
    assert "DENYLIST_PATH" in readme
    assert "Invalid settings abort startup" in readme
    assert "private/no-store" in readme
    assert "SnapshotReader" in readme
    assert "src/cu_rates/web" in readme

    # Must reference current.json (the implemented pointer file), never pointer.json
    assert "current.json" in readme
    assert "pointer.json" not in readme

    # Must document SnapshotPublisher.rollback()
    assert "SnapshotPublisher.rollback" in readme
    assert "publisher.rollback" in readme

    assert "Emergency Withdrawal Denylist" in readme
    assert "invalid `denylist.json` fails closed" in readme
    assert "denylist.json" in readme

    # Must document Denylist.withdraw() with required audit fields
    assert "Denylist.withdraw" in readme
    assert "denylist.withdraw" in readme
    assert "WithdrawalEntry" in readme
    required_audit_fields = [
        "withdrawal_id",
        "scope",
        "target_id",
        "actor",
        "withdrawn_at",
        "reason",
    ]
    for field in required_audit_fields:
        assert field in readme, f"README must document required audit field '{field}'"


def test_units_omit_tmp_from_readwritepaths_with_privatetmp() -> None:
    """Verify that neither service unit lists /tmp in ReadWritePaths when PrivateTmp=true."""
    service_files = [
        SYSTEMD_DIR / "cu-rates-web.service",
        SYSTEMD_DIR / "cu-rates-worker.service",
        SYSTEMD_DIR / "cu-rates-backup.service",
        SYSTEMD_DIR / "cu-rates-health.service",
        SYSTEMD_DIR / "cu-rates-quality.service",
    ]
    for sf in service_files:
        parsed = parse_systemd_unit(sf)
        sec = parsed.get("Service", {})
        assert sec.get("PrivateTmp") == ["true"], f"{sf.name} must have PrivateTmp=true"
        rw_entries = sec.get("ReadWritePaths", [])
        rw_paths = [p for entry in rw_entries for p in entry.split()]
        assert "/tmp" not in rw_paths, f"{sf.name} must not list /tmp in ReadWritePaths"


def test_readme_discloses_cu_rates_root_scope_and_hardcoded_unit_paths() -> None:
    """Verify README documents unit paths assume /var/lib/cu-rates and CU_RATES_ROOT scope."""
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")

    assert "CU_RATES_ROOT" in readme
    assert "/var/lib/cu-rates" in readme
    assert "assume the hardcoded production root path `/var/lib/cu-rates`" in readme
    assert (
        "`CU_RATES_ROOT` is supported by `deploy/install.sh` solely for installer testing" in readme
    )
    assert "does NOT rewrite or template the hardcoded unit paths" in readme


def test_gunicorn_port_is_consistent_and_avoids_the_known_busy_port() -> None:
    """Unit, Caddy and runbook agree on one loopback port that is not 8000 (taken on the Pi)."""
    unit = (DEPLOY_DIR / "systemd" / "cu-rates-web.service").read_text()
    caddy = (DEPLOY_DIR / "caddy" / "Caddyfile").read_text()
    readme = (DEPLOY_DIR / "README.md").read_text()
    assert "--bind 127.0.0.1:18200" in unit
    assert "reverse_proxy 127.0.0.1:18200" in caddy
    assert "127.0.0.1:18200" in readme
    for text in (unit, caddy, readme):
        assert "127.0.0.1:8000" not in text


def test_gunicorn_control_socket_is_disabled() -> None:
    """The read-only sandbox cannot host Gunicorn's control socket; it stays disabled."""
    unit = (DEPLOY_DIR / "systemd" / "cu-rates-web.service").read_text()
    assert "--no-control-socket" in unit


def test_runbook_withdrawal_example_actually_runs(tmp_path: Path) -> None:
    """The emergency withdrawal snippet in the runbook must execute (AUD7-11e)."""
    import re

    readme = (DEPLOY_DIR / "README.md").read_text()
    blocks = re.findall(r"```python\n(.*?)```", readme, flags=re.S)
    snippet = next(b for b in blocks if "denylist.withdraw(entry)" in b)
    ledger = tmp_path / "denylist.json"
    from cu_rates.snapshot import Denylist

    Denylist(ledger).initialize()
    import textwrap

    code = textwrap.dedent(snippet).replace(
        "/var/lib/cu-rates/independent/denylist.json", str(ledger)
    )
    exec(compile(code, "runbook-withdrawal-example", "exec"), {})
    assert "off-example-123" in ledger.read_text()


def test_worker_stopped_disabled_and_masked_per_d15_d17() -> None:
    """Verify worker units are stopped, disabled, masked (ln -sf /dev/null), and commit recorded."""
    install_sh = (DEPLOY_DIR / "install.sh").read_text(encoding="utf-8")

    # Worker units must be stopped
    assert "systemctl stop cu-rates-worker.service" in install_sh
    assert "systemctl stop cu-rates-worker.timer" in install_sh

    # Worker units must be disabled
    assert "systemctl disable cu-rates-worker.service" in install_sh
    assert "systemctl disable cu-rates-worker.timer" in install_sh

    # Worker units must be masked via symlink to /dev/null
    assert "ln -sf /dev/null" in install_sh
    assert 'ln -sf /dev/null "${SYSTEMD_DIR}/cu-rates-worker.service"' in install_sh
    assert 'ln -sf /dev/null "${SYSTEMD_DIR}/cu-rates-worker.timer"' in install_sh

    # Installed git commit must be recorded in root-owned state file (mode 0644)
    assert "installed-commit" in install_sh
    assert "0644" in install_sh
    assert "rev-parse HEAD" in install_sh


def test_install_script_dry_run_prints_worker_mask_and_commit(tmp_path: Path) -> None:
    """Verify install.sh --dry-run prints stopping, disabling, masking, and commit recording."""
    install_sh = DEPLOY_DIR / "install.sh"
    test_root = tmp_path / "cu-rates-dry-worker"
    res = subprocess.run(
        ["bash", str(install_sh), "--dry-run"],
        capture_output=True,
        text=True,
        env={**os.environ, "CU_RATES_ROOT": str(test_root)},
    )
    assert res.returncode == 0
    out = res.stdout
    assert "stop cu-rates-worker.service" in out
    assert "stop cu-rates-worker.timer" in out
    assert "cu-rates-worker.service is disabled" in out
    assert "cu-rates-worker.timer is disabled" in out
    assert "mask cu-rates-worker.service" in out
    assert "mask cu-rates-worker.timer" in out
    assert "installed-commit" in out
    assert "0644" in out


def test_independent_ledger_isolation_and_paths() -> None:
    """Verify independent ledger is moved to operator-owned dir read-only to worker and web."""
    worker_unit = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-worker.service")
    worker_sec = worker_unit.get("Service", {})
    worker_ro = set(" ".join(worker_sec.get("ReadOnlyPaths", [])).split())
    worker_rw = set(" ".join(worker_sec.get("ReadWritePaths", [])).split())

    # Worker has independent directory in ReadOnlyPaths, never in ReadWritePaths
    assert "/var/lib/cu-rates/independent" in worker_ro
    assert "/var/lib/cu-rates/independent" not in worker_rw

    # Web has independent directory in ReadOnlyPaths, never in ReadWritePaths
    web_unit = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-web.service")
    web_sec = web_unit.get("Service", {})
    web_ro = set(" ".join(web_sec.get("ReadOnlyPaths", [])).split())
    web_rw = set(" ".join(web_sec.get("ReadWritePaths", [])).split())
    assert "/var/lib/cu-rates/independent" in web_ro
    assert "/var/lib/cu-rates/independent" not in web_rw

    # env.example references /var/lib/cu-rates/independent/denylist.json
    env_content = (DEPLOY_DIR / "env.example").read_text(encoding="utf-8")
    assert "DENYLIST_PATH=/var/lib/cu-rates/independent/denylist.json" in env_content
    assert "/var/lib/cu-rates/snapshots/denylist.json" not in env_content

    # install.sh provisions operator-owned independent directory with mode 0755
    install_sh = (DEPLOY_DIR / "install.sh").read_text(encoding="utf-8")
    assert "independent" in install_sh
    assert "0755" in install_sh
    assert "verify_operator_directory" in install_sh

    # README documents independent directory
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")
    assert "/var/lib/cu-rates/independent" in readme
    assert "/var/lib/cu-rates/independent/denylist.json" in readme


def test_artifact_storage_mode_0700_and_ownership_verification() -> None:
    """Verify artifact storage is created mode 0700 (no group access) with ownership check."""
    install_sh = (DEPLOY_DIR / "install.sh").read_text(encoding="utf-8")

    # Artifact storage must be chmod 0700
    assert 'chmod 0700 "${NVME_ROOT}/artifacts"' in install_sh
    # Must NOT set 0750 on artifacts
    assert 'chmod 0750 "${NVME_ROOT}/data" "${NVME_ROOT}/artifacts"' not in install_sh

    # install.sh must verify ownership of artifacts directory
    assert "verify_artifact_ownership" in install_sh

    # README documents artifacts mode 0700
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")
    assert (
        "artifacts/            # Content-addressed raw evidence store (worker only, mode 0700)"
        in readme
    )


def test_install_script_rejects_unsafe_roots_and_symlinks(tmp_path: Path) -> None:
    """Verify install.sh rejects unsafe roots, symlinks, non-directories, and traversal."""
    install_sh = DEPLOY_DIR / "install.sh"

    # Reject system roots
    for unsafe_root in ["/", "/etc", "/usr", "/bin"]:
        res = subprocess.run(
            ["bash", str(install_sh)],
            capture_output=True,
            text=True,
            env={**os.environ, "CU_RATES_ROOT": unsafe_root},
        )
        assert res.returncode != 0, f"install.sh should reject unsafe root '{unsafe_root}'"
        assert "unsafe root" in res.stderr.lower()

    # Reject symlinks
    real_target = tmp_path / "real-dir"
    real_target.mkdir()
    symlink_target = tmp_path / "symlink-dir"
    symlink_target.symlink_to(real_target)

    res_sym = subprocess.run(
        ["bash", str(install_sh)],
        capture_output=True,
        text=True,
        env={**os.environ, "CU_RATES_ROOT": str(symlink_target)},
    )
    assert res_sym.returncode != 0, "install.sh should reject symlink root"
    assert "symlink" in res_sym.stderr.lower()

    # Reject regular file (not a real directory)
    regular_file = tmp_path / "some-file"
    regular_file.write_text("not a directory", encoding="utf-8")

    res_file = subprocess.run(
        ["bash", str(install_sh)],
        capture_output=True,
        text=True,
        env={**os.environ, "CU_RATES_ROOT": str(regular_file)},
    )
    assert res_file.returncode != 0, "install.sh should reject regular file as root"
    assert (
        "not a real directory" in res_file.stderr.lower()
        or "not a directory" in res_file.stderr.lower()
    )

    # Reject path traversal
    res_trav = subprocess.run(
        ["bash", str(install_sh)],
        capture_output=True,
        text=True,
        env={**os.environ, "CU_RATES_ROOT": f"{tmp_path}/foo/../bar"},
    )
    assert res_trav.returncode != 0, "install.sh should reject path traversal"
    assert "path traversal" in res_trav.stderr.lower()


def test_ops_systemd_units_exist_and_hardened() -> None:
    """Verify backup, health, and quality service units exist and enforce strict hardening."""
    ops_services = [
        SYSTEMD_DIR / "cu-rates-backup.service",
        SYSTEMD_DIR / "cu-rates-health.service",
        SYSTEMD_DIR / "cu-rates-quality.service",
    ]

    for service_file in ops_services:
        assert service_file.is_file(), f"{service_file.name} must exist"
        parsed = parse_systemd_unit(service_file)
        service_sec = parsed.get("Service", {})

        # User and unprivileged execution
        assert service_sec["User"] == ["cu-rates"]
        assert service_sec["Group"] == ["cu-rates"]
        assert service_sec["Type"] == ["oneshot"]
        assert service_sec["Restart"] == ["no"]
        assert service_sec["UMask"] == ["0077"]

        # Sandboxing and security hardening
        assert service_sec["NoNewPrivileges"] == ["true"]
        assert service_sec["PrivateTmp"] == ["true"]
        assert service_sec["ProtectSystem"] == ["strict"]
        assert service_sec["ProtectHome"] == ["true"]
        assert service_sec["PrivateDevices"] == ["true"]
        assert service_sec["ProtectClock"] == ["true"]
        assert service_sec["ProtectHostname"] == ["true"]
        assert service_sec["ProtectKernelTunables"] == ["true"]
        assert service_sec["ProtectKernelModules"] == ["true"]
        assert service_sec["ProtectControlGroups"] == ["true"]
        assert service_sec["RestrictNamespaces"] == ["true"]
        assert service_sec["RestrictRealtime"] == ["true"]
        assert service_sec["RestrictSUIDSGID"] == ["true"]
        assert service_sec["LockPersonality"] == ["true"]
        assert service_sec["ProtectProc"] == ["invisible"]
        assert service_sec["ProcSubset"] == ["pid"]
        assert service_sec["CapabilityBoundingSet"] == [""]
        assert service_sec["AmbientCapabilities"] == [""]
        assert service_sec["SystemCallArchitectures"] == ["native"]
        assert service_sec["SystemCallFilter"] == ["@system-service"]

        # No network: PrivateNetwork=true and AF_UNIX only
        assert service_sec["PrivateNetwork"] == ["true"]
        assert service_sec["RestrictAddressFamilies"] == ["AF_UNIX"]

        # StateDirectory must be absent (prevent broad write access)
        assert "StateDirectory" not in service_sec

        # Resource limits
        assert any("M" in limit or "G" in limit for limit in service_sec.get("MemoryMax", []))
        assert any(int(tasks) > 0 for tasks in service_sec.get("TasksMax", []))
        assert any("%" in quota for quota in service_sec.get("CPUQuota", []))


def test_ops_systemd_backup_and_health_filesystem_paths() -> None:
    """Verify backup writes only backups dir; health has no ReadWritePaths."""
    # Backup unit checks
    backup_unit = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-backup.service")
    backup_sec = backup_unit.get("Service", {})
    backup_rw = set(" ".join(backup_sec.get("ReadWritePaths", [])).split())
    backup_ro = set(" ".join(backup_sec.get("ReadOnlyPaths", [])).split())

    # The data directory is writable only so SQLite can create the -shm file for a WAL-mode
    # read-only open (see tests/test_ops_wal_readonly.py); the artifact store stays read-only.
    assert backup_rw == {"/var/lib/cu-rates/backups", "/var/lib/cu-rates/data"}, (
        f"backup service may write only backups and the WAL side files, got {backup_rw}"
    )
    assert "/var/lib/cu-rates/artifacts" in backup_ro
    assert "/var/lib/cu-rates/app" in backup_ro
    assert "/var/lib/cu-rates/artifacts" not in backup_rw
    assert "/var/lib/cu-rates/app" not in backup_rw

    # Verify backup ExecStart commands
    backup_exec = backup_sec.get("ExecStart", [])
    assert len(backup_exec) == 2, (
        f"backup service must have exactly 2 ExecStart commands, got {len(backup_exec)}"
    )
    assert "cu_rates.ops.backup create" in backup_exec[0]
    assert "/var/lib/cu-rates/data/operational.sqlite" in backup_exec[0]
    assert "/var/lib/cu-rates/artifacts" in backup_exec[0]
    assert "/var/lib/cu-rates/backups" in backup_exec[0]
    assert "cu_rates.ops.backup prune" in backup_exec[1]
    assert "--keep 14" in backup_exec[1]
    assert "/var/lib/cu-rates/backups" in backup_exec[1]

    # Health unit checks
    health_unit = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-health.service")
    health_sec = health_unit.get("Service", {})
    health_rw = set(" ".join(health_sec.get("ReadWritePaths", [])).split())
    assert health_rw == {"/var/lib/cu-rates/data"}, (
        f"health service may write only the WAL side files in the data directory, got {health_rw}"
    )

    health_ro = set(" ".join(health_sec.get("ReadOnlyPaths", [])).split())
    required_health_ro = {
        "/var/lib/cu-rates/app",
        "/var/lib/cu-rates/snapshots",
        "/var/lib/cu-rates/independent",
        "/var/lib/cu-rates/backups",
    }
    for path in required_health_ro:
        assert path in health_ro, f"health service missing {path} in ReadOnlyPaths"
    assert "/var/lib/cu-rates/data" not in health_ro  # it is the one writable path

    # Verify health ExecStart command
    health_exec = health_sec.get("ExecStart", [])
    assert len(health_exec) == 1
    assert "cu_rates.ops.health" in health_exec[0]
    assert "--snapshot-root /var/lib/cu-rates/snapshots" in health_exec[0]
    assert "--denylist /var/lib/cu-rates/independent/denylist.json" in health_exec[0]
    assert "--backup-dir /var/lib/cu-rates/backups" in health_exec[0]
    assert "--database /var/lib/cu-rates/data/operational.sqlite" in health_exec[0]


def test_ops_systemd_timers_calendar_and_persistence() -> None:
    """Verify backup and health timer units define OnCalendar and Persistent=true."""
    # Backup timer
    backup_timer = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-backup.timer")
    b_timer_sec = backup_timer.get("Timer", {})
    assert b_timer_sec.get("OnCalendar") == ["*-*-* 03:17:00"]
    assert b_timer_sec.get("Persistent") == ["true"]
    assert b_timer_sec.get("Unit") == ["cu-rates-backup.service"]
    assert "RandomizedDelaySec" in b_timer_sec
    assert backup_timer.get("Install", {}).get("WantedBy") == ["timers.target"]

    # Health timer
    health_timer = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-health.timer")
    h_timer_sec = health_timer.get("Timer", {})
    assert h_timer_sec.get("OnCalendar") == ["*:00/15:00"]
    assert h_timer_sec.get("Persistent") == ["true"]
    assert h_timer_sec.get("Unit") == ["cu-rates-health.service"]
    assert health_timer.get("Install", {}).get("WantedBy") == ["timers.target"]


def test_ops_systemd_install_script_wiring_and_masking() -> None:
    """Verify install.sh installs ops units, does not enable them, and masks worker units."""
    install_sh = (DEPLOY_DIR / "install.sh").read_text(encoding="utf-8")

    # install.sh references all scheduled ops units in UNITS array
    for unit in [
        "cu-rates-backup.service",
        "cu-rates-backup.timer",
        "cu-rates-health.service",
        "cu-rates-health.timer",
        "cu-rates-quality.service",
        "cu-rates-quality.timer",
    ]:
        assert f'"{unit}"' in install_sh, f"install.sh must list {unit} in UNITS array"

    # install.sh must NOT enable the backup, health, or quality units
    assert "systemctl enable cu-rates-backup" not in install_sh
    assert "systemctl enable cu-rates-health" not in install_sh
    assert "systemctl enable cu-rates-quality" not in install_sh

    # Worker units MUST still be masked
    assert 'ln -sf /dev/null "${SYSTEMD_DIR}/cu-rates-worker.service"' in install_sh
    assert 'ln -sf /dev/null "${SYSTEMD_DIR}/cu-rates-worker.timer"' in install_sh
    assert "systemctl disable cu-rates-worker.service" in install_sh
    assert "systemctl disable cu-rates-worker.timer" in install_sh
    assert "systemctl stop cu-rates-worker.service" in install_sh
    assert "systemctl stop cu-rates-worker.timer" in install_sh


def test_ops_readme_scheduled_operations_section() -> None:
    """Verify README documents scheduled operations, enable commands, and required notices."""
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")

    assert "Scheduled Operations" in readme
    assert "cu-rates-backup.timer" in readme
    assert "cu-rates-health.timer" in readme
    assert "systemctl enable --now cu-rates-backup.timer cu-rates-health.timer" in readme
    assert "systemctl list-timers" in readme
    assert "journalctl -u cu-rates-health" in readme
    assert "journalctl -u cu-rates-backup" in readme
    notice = "off-device encrypted copies and alert delivery are not implemented yet"
    assert notice in readme.lower()


def test_ops_negative_control_health_readwritepaths_rejected() -> None:
    """Negative control: the health unit may write ONLY the data directory (WAL side files)."""
    parsed = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-health.service")
    rw = set(" ".join(parsed.get("Service", {}).get("ReadWritePaths", [])).split())
    assert rw == {"/var/lib/cu-rates/data"}
    assert rw != rw | {"/var/lib/cu-rates/snapshots"}  # anything more is a regression


def test_ops_negative_control_backup_timer_persistence_required() -> None:
    """Negative control: backup timer fails if Persistent=true is missing."""
    timer_text = (SYSTEMD_DIR / "cu-rates-backup.timer").read_text(encoding="utf-8")
    assert "Persistent=true" in timer_text

    corrupted = timer_text.replace("Persistent=true", "")
    with pytest.raises(AssertionError):
        assert "Persistent=true" in corrupted


def test_ops_systemd_quality_filesystem_paths_and_execstart() -> None:
    """Verify quality unit filesystem paths and ExecStart command."""
    quality_unit = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-quality.service")
    quality_sec = quality_unit.get("Service", {})
    quality_rw = set(" ".join(quality_sec.get("ReadWritePaths", [])).split())
    assert quality_rw == {"/var/lib/cu-rates/data"}, (
        f"quality service may write only the WAL side files in the data directory, got {quality_rw}"
    )

    quality_ro = set(" ".join(quality_sec.get("ReadOnlyPaths", [])).split())
    assert "/var/lib/cu-rates/app" in quality_ro
    assert "/var/lib/cu-rates/data" not in quality_ro

    quality_exec = quality_sec.get("ExecStart", [])
    assert len(quality_exec) == 1
    assert (
        "cu_rates.ops.quality --database /var/lib/cu-rates/data/operational.sqlite"
        in quality_exec[0]
    )
    assert quality_exec[0].startswith("/var/lib/cu-rates/venv/bin/python")
    assert not quality_exec[0].startswith("-")
    assert "|| true" not in quality_exec[0]


def test_ops_systemd_quality_timer_calendar_and_persistence() -> None:
    """Verify quality timer unit defines OnCalendar and Persistent=true."""
    quality_timer = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-quality.timer")
    q_timer_sec = quality_timer.get("Timer", {})
    assert q_timer_sec.get("OnCalendar") == ["*-*-* 06:11:00"]
    assert q_timer_sec.get("Persistent") == ["true"]
    assert q_timer_sec.get("Unit") == ["cu-rates-quality.service"]
    assert q_timer_sec.get("RandomizedDelaySec") == ["900"]
    assert quality_timer.get("Install", {}).get("WantedBy") == ["timers.target"]


def test_ops_negative_control_quality_readwritepaths_rejected() -> None:
    """Negative control: the quality unit may write ONLY the data directory (WAL side files)."""
    parsed = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-quality.service")
    rw = set(" ".join(parsed.get("Service", {}).get("ReadWritePaths", [])).split())
    assert rw == {"/var/lib/cu-rates/data"}
    assert rw != rw | {"/var/lib/cu-rates/snapshots"}


def test_ops_negative_control_quality_timer_persistence_required() -> None:
    """Negative control: quality timer fails if Persistent=true is missing."""
    timer_text = (SYSTEMD_DIR / "cu-rates-quality.timer").read_text(encoding="utf-8")
    assert "Persistent=true" in timer_text

    corrupted = timer_text.replace("Persistent=true", "")
    with pytest.raises(AssertionError):
        assert "Persistent=true" in corrupted


def test_ops_readme_quality_scheduled_operations_section() -> None:
    """Verify README documents quality timer in scheduled operations section."""
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")
    assert "cu-rates-quality.timer" in readme
    assert "cu-rates-quality.service" in readme
    assert "cu_rates.ops.quality --database /var/lib/cu-rates/data/operational.sqlite" in readme
    assert "exit status 1" in readme
    assert "journalctl -u cu-rates-quality" in readme
    notice = "alert delivery is not implemented yet"
    assert notice in readme.lower()


def test_correction_inbox_is_wired_closed_by_default_and_least_privilege() -> None:
    """#100: the form may only add to its own directory, and stays closed until INTAKE_DB is set."""
    web = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-web.service").get("Service", {})
    web_rw = set(" ".join(web.get("ReadWritePaths", [])).split())
    assert web_rw == {"/run/cu-rates-web", "/var/lib/cu-rates/intake"}
    web_blocked = " ".join(web.get("InaccessiblePaths", []))
    for private in (
        "/var/lib/cu-rates/data",
        "/var/lib/cu-rates/artifacts",
        "/var/lib/cu-rates/backups",
    ):
        assert private in web_blocked  # the form still cannot reach the worker's storage

    env = (DEPLOY_DIR / "env.example").read_text(encoding="utf-8")
    assert "# INTAKE_DB=/var/lib/cu-rates/intake/intake.sqlite" in env  # commented out: closed
    assert not any(line.startswith("INTAKE_DB=") for line in env.splitlines())
    assert "within one business day" in env

    purge = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-intake-purge.service").get("Service", {})
    assert purge["User"] == ["cu-rates"] and purge["NoNewPrivileges"] == ["true"]
    assert purge["PrivateNetwork"] == ["true"] and purge["UMask"] == ["0077"]
    assert set(" ".join(purge["ReadWritePaths"]).split()) == {"/var/lib/cu-rates/intake"}
    assert (
        "cu_rates.intake --db /var/lib/cu-rates/intake/intake.sqlite purge-contacts"
        in purge["ExecStart"][0]
    )
    timer = (SYSTEMD_DIR / "cu-rates-intake-purge.timer").read_text(encoding="utf-8")
    assert "Persistent=true" in timer and "OnCalendar=" in timer

    install = (DEPLOY_DIR / "install.sh").read_text(encoding="utf-8")
    assert '"${NVME_ROOT}/intake"' in install and "0700" in install
    assert "cu-rates-intake-purge.timer" in install
    enabled = [
        line for line in install.splitlines() if "run_action" in line and "systemctl enable" in line
    ]
    assert enabled == [] or all("cu-rates-web.service" in line for line in enabled)
    actions = install.split("run_action")
    assert not any(
        "enable" in chunk.splitlines()[0] and "purge" in chunk.splitlines()[0]
        for chunk in actions[1:]
    )  # the purge timer is installed for the operator to enable, never enabled by the installer
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")
    assert "Correction Inbox" in readme and "bulk-reject" in readme and "INTAKE_DB" in readme


def test_automatic_suppression_ledger_is_worker_writable_and_never_the_operator_ledger() -> None:
    """#118: the worker records its own blocks in a separate ledger; the operator one stays RO."""
    worker = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-worker.service").get("Service", {})
    web = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-web.service").get("Service", {})
    health = parse_systemd_unit(SYSTEMD_DIR / "cu-rates-health.service").get("Service", {})
    paths = {
        name: (
            set(" ".join(unit.get("ReadWritePaths", [])).split()),
            # A leading "-" only means "do not fail if the directory is absent".
            {p.removeprefix("-") for p in " ".join(unit.get("ReadOnlyPaths", [])).split()},
        )
        for name, unit in (("worker", worker), ("web", web), ("health", health))
    }
    automatic, operator = "/var/lib/cu-rates/automatic", "/var/lib/cu-rates/independent"
    assert automatic in paths["worker"][0] and operator not in paths["worker"][0]
    assert operator in paths["worker"][1]  # the operator ledger stays read-only to the worker
    assert automatic in paths["web"][1] and automatic not in paths["web"][0]
    assert automatic in paths["health"][1] and automatic not in paths["health"][0]
    assert (
        "--automatic-denylist /var/lib/cu-rates/automatic/denylist.json" in health["ExecStart"][0]
    )
    env = (DEPLOY_DIR / "env.example").read_text(encoding="utf-8")
    assert "AUTOMATIC_DENYLIST_PATH=/var/lib/cu-rates/automatic/denylist.json" in env
    assert '"${NVME_ROOT}/automatic"' in (DEPLOY_DIR / "install.sh").read_text(encoding="utf-8")


def test_worker_wiring_is_documented_and_the_worker_unit_is_still_the_disabled_stub() -> None:
    """#115: naming the worker's paths must not enable it."""
    env = (DEPLOY_DIR / "env.example").read_text(encoding="utf-8")
    for name in ("OPERATIONAL_DB", "TERMS_LEDGER_PATH", "WORKER_STATE_DIR", "ARTIFACT_ROOT"):
        assert f"# {name}=/var/lib/cu-rates/" in env  # documented, commented out
        assert not any(line.startswith(f"{name}=") for line in env.splitlines())
    assert "python -m cu_rates.worker_config check" in env
    worker = (SYSTEMD_DIR / "cu-rates-worker.service").read_text(encoding="utf-8")
    assert "Worker execution disabled per MASTER.md D-15" in worker
    assert "cu_rates.worker_config" not in worker  # the config check is not a way to start it
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")
    assert "Worker wiring check" in readme and "read-only" in readme
