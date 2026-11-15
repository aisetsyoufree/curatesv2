import re
import sys

import cu_rates


def test_package_imports_with_version() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", cu_rates.__version__)


def test_runs_on_pinned_python() -> None:
    assert sys.version_info[:2] == (3, 12)
