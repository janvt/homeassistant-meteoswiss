"""The rules of the quality scale, against Home Assistant's latest.

tests/test_quality_scale.py checks quality_scale.yaml against its own copy of
the rules, by tier.  This compares that copy with the rules hassfest checks
core integrations against, so that a rule Home Assistant adds, removes or
moves to another tier fails here (see .github/workflows/live.yaml) rather
than going unnoticed.
"""

from __future__ import annotations

import asyncio
import os
import re

import aiohttp

from tests.test_quality_scale import TIERS

HASSFEST = (
    "https://raw.githubusercontent.com/home-assistant/core/dev/"
    "script/hassfest/quality_scale.py"
)
# Rule("action-setup", ScaledQualityScaleTiers.BRONZE), possibly over lines.
RULE = re.compile(r'Rule\(\s*"([a-z0-9-]+)",\s*ScaledQualityScaleTiers\.([A-Z]+)\b')
# GitHub limits downloads without a token by IP address, which the runners
# share: on 16 November 2026 it answered 429.  The workflow's token raises the
# limit, and a 429 is retried after the wait GitHub asks for.
ATTEMPTS = 4


async def fetch_hassfest(http: aiohttp.ClientSession) -> str:
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for attempt in range(ATTEMPTS):
        async with http.get(HASSFEST, headers=headers) as response:
            if response.status != 429 or attempt == ATTEMPTS - 1:
                response.raise_for_status()
                return await response.text()
            wait = response.headers.get("Retry-After", "")
        await asyncio.sleep(min(int(wait) if wait.isdigit() else 10 * 2**attempt, 60))
    raise AssertionError("unreachable")


async def test_the_rules_are_home_assistants_latest(http):
    source = await fetch_hassfest(http)
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
