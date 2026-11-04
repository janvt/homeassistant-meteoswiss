"""Nothing the integration imports is on its way out of Home Assistant."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

# In its own Python, since a module the tests imported is not imported again.
IMPORT_ALL = """
import importlib, logging, pkgutil, warnings

import custom_components.meteoswiss as integration

logging.basicConfig(level=logging.WARNING)
warnings.filterwarnings(
    "error", category=DeprecationWarning, module=r"custom_components\\."
)
for module in pkgutil.iter_modules(integration.__path__):
    importlib.import_module(f"{integration.__name__}.{module.name}")
"""


def test_importing_the_integration_uses_nothing_deprecated():
    done = subprocess.run(
        [sys.executable, "-c", IMPORT_ALL],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    # Home Assistant logs what an integration uses, rather than warning.
    lines = done.stderr.splitlines()
    assert not [line for line in lines if "from meteoswiss" in line]
