"""Tests for toolchain pins and workflow security configurations."""

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "check.yml"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

HEX_40_RE = re.compile(r"^[0-9a-fA-F]{40}$")
EXACT_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def parse_workflow_steps(workflow_text: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    lines = workflow_text.splitlines()
    in_steps = False
    current_step: dict[str, Any] | None = None
    current_section: str | None = None

    for raw_line in lines:
        line = raw_line.split(" #")[0].rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip())

        if stripped == "steps:":
            in_steps = True
            continue

        if not in_steps:
            continue

        if indent < 6 and not stripped.startswith("-"):
            in_steps = False
            continue

        if stripped.startswith("- "):
            if current_step is not None:
                steps.append(current_step)
            current_step = {}
            current_section = None
            item = stripped[2:].strip()
            if ":" in item:
                k, v = item.split(":", 1)
                current_step[k.strip()] = v.strip().strip('"').strip("'")
        elif current_step is not None:
            if stripped.endswith(":") and not stripped.startswith("-"):
                current_section = stripped[:-1].strip()
                current_step[current_section] = {}
            elif current_section is not None and ":" in stripped:
                k, v = stripped.split(":", 1)
                k_clean = k.strip()
                v_clean = v.strip().strip('"').strip("'")
                val: Any = v_clean
                if v_clean.lower() == "false":
                    val = False
                elif v_clean.lower() == "true":
                    val = True
                current_step[current_section][k_clean] = val
            elif ":" in stripped:
                current_section = None
                k, v = stripped.split(":", 1)
                current_step[k.strip()] = v.strip().strip('"').strip("'")

    if current_step is not None:
        steps.append(current_step)

    return steps


def extract_uses_references(workflow_text: str) -> list[str]:
    uses_list: list[str] = []
    for line in workflow_text.splitlines():
        match = re.match(r"^\s*-\s*uses:\s*([^\s#]+)", line)
        if match:
            uses_list.append(match.group(1))
    return uses_list


def validate_uses_shas(uses_list: list[str]) -> None:
    assert uses_list, "No uses: directives found in workflow"
    for ref in uses_list:
        assert "@" in ref, f"Action reference {ref!r} does not contain an '@' separator"
        action, _, sha = ref.partition("@")
        assert HEX_40_RE.fullmatch(sha), (
            f"Action {action!r} must be pinned to a 40-character hex SHA, got {sha!r}"
        )


def validate_checkout_credentials(steps: list[dict[str, Any]]) -> None:
    checkout_steps = [s for s in steps if s.get("uses", "").split("@")[0] == "actions/checkout"]
    assert checkout_steps, "No actions/checkout step found in workflow"
    for step in checkout_steps:
        with_block = step.get("with", {})
        assert "persist-credentials" in with_block, (
            "actions/checkout missing persist-credentials configuration"
        )
        val = with_block["persist-credentials"]
        assert val is False, f"actions/checkout persist-credentials must be false, got {val!r}"


def validate_setup_uv_version(steps: list[dict[str, Any]]) -> None:
    setup_uv_steps = [s for s in steps if s.get("uses", "").split("@")[0] == "astral-sh/setup-uv"]
    assert setup_uv_steps, "No astral-sh/setup-uv step found in workflow"
    for step in setup_uv_steps:
        with_block = step.get("with", {})
        assert "version" in with_block, "astral-sh/setup-uv missing version configuration"
        version_str = str(with_block["version"]).strip()
        assert EXACT_VERSION_RE.fullmatch(version_str), (
            f"setup-uv version must be an exact semver (X.Y.Z), got {version_str!r}"
        )


def validate_uv_build_pin(pyproject_text: str) -> None:
    data = tomllib.loads(pyproject_text)
    build_system = data.get("build-system")
    assert isinstance(build_system, dict), "[build-system] section missing from pyproject.toml"
    requires = build_system.get("requires")
    assert isinstance(requires, list) and requires, (
        "[build-system].requires missing or empty in pyproject.toml"
    )

    uv_build_reqs = [
        r
        for r in requires
        if isinstance(r, str) and (r.startswith("uv_build") or r.startswith("uv-build"))
    ]
    assert uv_build_reqs, "No uv_build requirement found in [build-system].requires"

    for req in uv_build_reqs:
        exact_match = re.fullmatch(r"uv[-_]build==(\d+\.\d+\.\d+)", req)
        if exact_match:
            continue

        range_match = re.fullmatch(
            r"uv[-_]build>=\s*(\d+)\.(\d+)(?:\.\d+)?\s*,\s*<\s*(\d+)\.(\d+)(?:\.0)?", req
        )
        if range_match:
            min_major, min_minor, max_major, max_minor = map(int, range_match.groups())
            if min_major == max_major and max_minor == min_minor + 1:
                continue

        raise AssertionError(f"uv_build must be an exact pin or a one-minor range, got {req!r}")


def test_workflow_uses_full_40_hex_shas() -> None:
    workflow_text = CHECK_WORKFLOW_PATH.read_text(encoding="utf-8")
    uses_list = extract_uses_references(workflow_text)
    validate_uses_shas(uses_list)


def test_workflow_checkout_persist_credentials_false() -> None:
    workflow_text = CHECK_WORKFLOW_PATH.read_text(encoding="utf-8")
    steps = parse_workflow_steps(workflow_text)
    validate_checkout_credentials(steps)


def test_workflow_setup_uv_exact_version() -> None:
    workflow_text = CHECK_WORKFLOW_PATH.read_text(encoding="utf-8")
    steps = parse_workflow_steps(workflow_text)
    validate_setup_uv_version(steps)


def test_pyproject_uv_build_pin() -> None:
    pyproject_text = PYPROJECT_PATH.read_text(encoding="utf-8")
    validate_uv_build_pin(pyproject_text)


@pytest.mark.parametrize(
    "invalid_ref",
    [
        "actions/checkout@v7.0.1",
        "actions/checkout@main",
        "actions/checkout@12345",
        "actions/checkout",
    ],
)
def test_fails_if_uses_sha_is_removed_or_not_40_hex(invalid_ref: str) -> None:
    with pytest.raises(AssertionError):
        validate_uses_shas([invalid_ref])


@pytest.mark.parametrize(
    "invalid_step",
    [
        {"uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"},
        {
            "uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            "with": {"persist-credentials": True},
        },
    ],
)
def test_fails_if_checkout_persist_credentials_missing_or_true(
    invalid_step: dict[str, Any],
) -> None:
    with pytest.raises(AssertionError):
        validate_checkout_credentials([invalid_step])


@pytest.mark.parametrize(
    "invalid_step",
    [
        {"uses": "astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2"},
        {
            "uses": "astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2",
            "with": {"version": "latest"},
        },
        {
            "uses": "astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2",
            "with": {"version": ">=0.11"},
        },
    ],
)
def test_fails_if_setup_uv_version_missing_or_not_exact(
    invalid_step: dict[str, Any],
) -> None:
    with pytest.raises(AssertionError):
        validate_setup_uv_version([invalid_step])


@pytest.mark.parametrize(
    "invalid_req",
    [
        'requires = ["uv_build"]',
        'requires = ["uv_build>=0.11.16"]',
        'requires = ["uv_build>=0.11.16,<0.14"]',
        'requires = ["uv_build>=0.11.16,<1.0"]',
        "requires = []",
    ],
)
def test_fails_if_uv_build_unpinned_or_multi_minor(invalid_req: str) -> None:
    toml_str = f"""
    [build-system]
    {invalid_req}
    build-backend = "uv_build"
    """
    with pytest.raises(AssertionError):
        validate_uv_build_pin(toml_str)
