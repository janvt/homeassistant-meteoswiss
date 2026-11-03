"""The tests run with the requirements Home Assistant installs."""

import json
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_manifest_requirements_are_tested():
    manifest = json.loads(
        (ROOT / "custom_components" / "meteoswiss" / "manifest.json").read_text()
    )
    tested = {
        line.split("#")[0].strip()
        for line in (ROOT / "requirements_test.txt").read_text().splitlines()
    }
    # Dependabot updates the pins of the tests only.
    assert set(manifest["requirements"]) <= tested
