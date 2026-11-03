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


def exception_keys_raised() -> set[str]:
    """The translation keys of the errors raised with a translation domain."""
    keys = set()
    for module in COMPONENT.glob("*.py"):
        for node in ast.walk(ast.parse(module.read_text())):
            if isinstance(node, ast.Call):
                kwargs = {k.arg: k.value for k in node.keywords}
                key = kwargs.get("translation_key")
                if "translation_domain" in kwargs and isinstance(key, ast.Constant):
                    keys.add(key.value)
    return keys


def test_errors_have_texts():
    raised = exception_keys_raised()
    assert {"update_failed", "unknown_place"} <= raised  # the parsing found them
    assert raised <= set(EN["exceptions"])


ICONS = json.loads((COMPONENT / "icons.json").read_text())


def test_icons_come_from_icons_json():
    """None set in code, where they could not depend on the state."""
    for module in COMPONENT.glob("*.py"):
        for node in ast.walk(ast.parse(module.read_text())):
            assert not (isinstance(node, ast.keyword) and node.arg == "icon"), module
            assert not (isinstance(node, ast.Name) and node.id == "_attr_icon"), module
            assert not (isinstance(node, ast.FunctionDef) and node.name == "icon"), (
                module
            )


def test_icons_are_material_design_icons():
    def icons(value):
        if isinstance(value, dict):
            for v in value.values():
                yield from icons(v)
        else:
            yield value

    assert all(icon.startswith("mdi:") for icon in icons(ICONS))


def test_every_action_has_an_icon():
    import yaml

    actions = yaml.safe_load((COMPONENT / "services.yaml").read_text())
    assert set(ICONS["services"]) == set(actions)
