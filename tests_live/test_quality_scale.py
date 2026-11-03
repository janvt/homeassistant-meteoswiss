"""The rules of the quality scale, against Home Assistant's latest.

tests/test_quality_scale.py checks quality_scale.yaml against its own copy of
the rules, by tier.  This compares that copy with the rules hassfest checks
core integrations against, so that a rule Home Assistant adds, removes or
moves to another tier fails here (see .github/workflows/live.yaml) rather
than going unnoticed.
"""

from __future__ import annotations

import re

from tests.test_quality_scale import TIERS

HASSFEST = (
    "https://raw.githubusercontent.com/home-assistant/core/dev/"
    "script/hassfest/quality_scale.py"
)
# Rule("action-setup", ScaledQualityScaleTiers.BRONZE), possibly over lines.
RULE = re.compile(r'Rule\(\s*"([a-z0-9-]+)",\s*ScaledQualityScaleTiers\.([A-Z]+)\b')


async def test_the_rules_are_home_assistants_latest(http):
    async with http.get(HASSFEST) as response:
        response.raise_for_status()
        source = await response.text()
    latest = {rule: tier.lower() for rule, tier in RULE.findall(source)}
    # Rather than every rule seeming removed if hassfest is written otherwise.
    assert len(latest) > 40, f"Cannot read the rules from {HASSFEST}"

    ours = {rule: tier for tier, rules in TIERS.items() for rule in rules}
    changes = [f"added {r} ({latest[r]})" for r in sorted(latest.keys() - ours)]
    changes += [f"removed {r}" for r in sorted(ours.keys() - latest)]
    changes += [
        f"moved {r} from {ours[r]} to {latest[r]}"
        for r in sorted(ours.keys() & latest.keys())
        if ours[r] != latest[r]
    ]
    assert not changes, (
        "The quality scale changed; update TIERS in tests/test_quality_scale.py "
        f"and quality_scale.yaml: {', '.join(changes)}"
    )
