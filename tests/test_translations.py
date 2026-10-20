"""Every message the integration can show has an English text."""

import ast
import json
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parent.parent / "custom_components" / "meteoswiss"
EN = json.loads((COMPONENT / "translations" / "en.json").read_text())


def error_keys_raised() -> set[str]:
    """The string values assigned to errors[...] in the config flow."""
    tree = ast.parse((COMPONENT / "config_flow.py").read_text())
    keys = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == "errors"
            and isinstance(node.value, ast.Constant)
        ):
            keys.add(node.value.value)
    return keys


def test_config_flow_errors_have_texts():
    raised = error_keys_raised()
    assert raised  # the parsing above found them
    # The options flow is in config_flow.py too.
    assert raised <= set(EN["config"]["error"]) | set(EN["options"]["error"])


@pytest.mark.parametrize("path", ["config", "options"])
def test_data_descriptions_have_labels(path):
    for step in EN[path]["step"].values():
        assert set(step.get("data_description", {})) <= set(step.get("data", {}))


# Romansh falls back to English for what it does not translate.
PARTIAL = {"rm"}


@pytest.mark.parametrize(
    "language",
    sorted(p.stem for p in (COMPONENT / "translations").glob("*.json")),
)
def test_translations_are_valid_json_with_the_same_steps(language):
    translation = json.loads(
        (COMPONENT / "translations" / f"{language}.json").read_text()
    )
    steps = set(translation["config"]["step"])
    if language in PARTIAL:
        assert steps <= set(EN["config"]["step"])
    else:
        assert steps == set(EN["config"]["step"])
        assert set(translation["issues"]) == set(EN["issues"])
        assert set(translation["config"]["error"]) == set(EN["config"]["error"])


def test_menu_options_have_texts():
    import re

    flow = (COMPONENT / "config_flow.py").read_text()
    menus = re.findall(r'step_id="(\w+)", menu_options=\[([^\]]*)\]', flow)
    assert menus  # the pattern above found the menu
    for step_id, options in menus:
        names = set(re.findall(r'"(\w+)"', options))
        assert names == set(EN["config"]["step"][step_id]["menu_options"])
