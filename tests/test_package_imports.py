"""Package mode import checks for the plugin's own modules.

Every module in this repository imports its siblings through the same
`try: from .x import y / except ImportError: from x import y` pair, so a host may
load them either as top-level modules from the checkout root or as members of a
package.

`closed_loop` and `approval_review` each carried a bare `from ledger import
tail_lines` and `from approval_policy import apply_policy` ABOVE that `try`, so
they worked only in the first mode. In package mode they raised
`ModuleNotFoundError: No module named 'ledger'`, naming a module that exists
one directory up, which is exactly the misleading error the `try` exists to
avoid.

The check copies the repository into a temporary directory, makes it an
importable package, puts only the PARENT on `sys.path`, and imports each module
as a package member. The checkout root is deliberately not importable, so a flat
import cannot succeed by accident.
"""
from __future__ import annotations

import shutil
import sys
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Every module that must import cleanly in both modes.
MODULES = [
    "approval_policy",
    "approval_review",
    "closed_loop",
    "cockpit",
    "fabric",
    "gateway",
    "ingest",
    "jev_case",
    "jev_client",
    "ledger",
    "lessons",
    "runtime",
    "shadow_report",
    "supervision",
    "verification",
]

PROBE = textwrap.dedent(
    """
    import importlib
    import sys

    parent, checkout_root = sys.argv[1], sys.argv[2]
    # Keep the interpreter's own standard library entries, then force the
    # checkout root out of the path: only the PARENT of the package is
    # importable, so a flat `from ledger import ...` cannot resolve by accident.
    sys.path = [parent] + [
        entry for entry in sys.path
        if entry and entry not in (".", checkout_root)
    ]

    for name in sys.argv[3:]:
        try:
            importlib.import_module("jevpkg." + name)
        except Exception as exc:
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            break
    else:
        print("ALL_OK")
    """
)


def _package_copy(tmp_path: Path) -> Path:
    """Copy the repository to `<tmp>/jevpkg` as an importable package."""
    target = tmp_path / "jevpkg"
    shutil.copytree(
        REPO,
        target,
        ignore=shutil.ignore_patterns(
            "__pycache__", ".git", "build", "*.egg-info", ".pytest_cache"
        ),
    )
    # `__init__.py` is the plugin entry point, not a package marker, so move it
    # aside and leave an empty package init behind.
    shutil.move(str(target / "__init__.py"), str(target / "plugin_init.py"))
    (target / "__init__.py").write_text("", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("module", MODULES)
def test_a_module_imports_when_the_host_uses_it_as_a_package(tmp_path, module):
    """One module per process: `closed_loop` and `approval_review` are exactly
    the pair that regressed, and a shared process would hide which one failed."""
    parent = _package_copy(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", PROBE, str(parent), str(REPO), module],
        capture_output=True, text=True, timeout=180,
    )
    assert "ALL_OK" in result.stdout, (
        f"importing jevpkg.{module} as a package failed:\n"
        f"{result.stdout.strip()}\n{result.stderr.strip()[-800:]}"
    )
