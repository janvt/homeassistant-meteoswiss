"""quality_scale.yaml, checked as hassfest checks it for core integrations."""

import importlib
import json
from pathlib import Path

import pytest
import yaml

from custom_components.meteoswiss import PLATFORMS

INTEGRATION = Path(__file__).parent.parent / "custom_components" / "meteoswiss"

# https://contributors.home-assistant.io/core/quality-scale/rules/
TIERS = {
    "bronze": (
        "action-setup",
        "appropriate-polling",
        "brands",
        "common-modules",
        "config-flow",
        "config-flow-test-coverage",
        "dependency-transparency",
        "docs-actions",
        "docs-conditions",
        "docs-high-level-description",
        "docs-installation-instructions",
        "docs-removal-instructions",
        "docs-triggers",
        "entity-event-setup",
        "entity-unique-id",
        "has-entity-name",
        "runtime-data",
        "test-before-configure",
        "test-before-setup",
        "unique-config-entry",
    ),
    "silver": (
        "action-exceptions",
        "config-entry-unloading",
        "docs-configuration-parameters",
        "docs-installation-parameters",
        "entity-unavailable",
        "integration-owner",
        "log-when-unavailable",
        "parallel-updates",
        "reauthentication-flow",
        "test-coverage",
    ),
    "gold": (
        "devices",
        "diagnostics",
        "discovery",
        "discovery-update-info",
        "docs-data-update",
        "docs-examples",
        "docs-known-limitations",
        "docs-supported-devices",
        "docs-supported-functions",
        "docs-troubleshooting",
        "docs-use-cases",
        "dynamic-devices",
        "entity-category",
        "entity-device-class",
        "entity-disabled-by-default",
        "entity-translations",
        "exception-translations",
        "icon-translations",
        "reconfiguration-flow",
        "repair-issues",
        "stale-devices",
    ),
    "platinum": (
        "async-dependency",
        "inject-websession",
        "strict-typing",
    ),
}
STATUSES = {"done", "todo", "exempt"}


def rules() -> dict[str, dict[str, str]]:
    """Each rule as a status and a comment, whichever way it is written."""
    listed = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    return {
        rule: value if isinstance(value, dict) else {"status": value}
        for rule, value in listed.items()
    }


def test_every_rule_is_listed_once():
    assert sorted(rules()) == sorted(r for tier in TIERS.values() for r in tier)


@pytest.mark.parametrize("rule", sorted(r for tier in TIERS.values() for r in tier))
def test_each_rule_has_a_status(rule):
    value = rules()[rule]
    assert set(value) <= {"status", "comment"}
    assert value["status"] in STATUSES
    if value["status"] == "exempt":
        assert value.get("comment"), "an exemption needs a reason"


def test_the_declared_tier_is_met():
    """Every rule up to the tier in the manifest, if any, is done or exempt."""
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    tier = manifest.get("quality_scale")
    if tier is None:
        pytest.skip("No tier declared yet")
    assert tier in TIERS
    names = list(TIERS)
    open_rules = [
        rule
        for name in names[: names.index(tier) + 1]
        for rule in TIERS[name]
        if rules()[rule]["status"] == "todo"
    ]
    assert not open_rules, f"{tier} needs {open_rules}"


@pytest.mark.parametrize("platform", PLATFORMS)
def test_every_platform_sets_its_parallel_updates(platform):
    """parallel-updates, as hassfest checks it."""
    module = importlib.import_module(f"custom_components.meteoswiss.{platform}")
    assert isinstance(getattr(module, "PARALLEL_UPDATES", None), int)


def test_the_readme_badge_shows_the_declared_tier():
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    readme = (INTEGRATION.parent.parent / "README.md").read_text()
    tier = manifest.get("quality_scale")
    assert (f"badge/quality%20scale-{tier}-" in readme) == (tier is not None)
