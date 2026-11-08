import json
import re
from pathlib import Path

import pytest

from realmebuds import cli
from realmebuds.profiles import profile_from_dict

DOCS = Path(__file__).resolve().parent.parent / "docs"


def first_json_block(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"```json\n(.*?)```", text, re.S)
    assert match, f"no json block in {path.name}"
    return json.loads(match.group(1))


def test_the_example_profile_in_the_guide_is_valid():
    profile = profile_from_dict(first_json_block(DOCS / "ADDING_A_DEVICE.md"), "ADDING_A_DEVICE.md")
    assert profile.key == "example-buds" and profile.mode("off").report == 8 and profile.hold_min == 2


def test_the_guide_does_not_name_commands_that_do_not_exist():
    text = (DOCS / "ADDING_A_DEVICE.md").read_text(encoding="utf-8")
    parser = cli.build_parser()
    for command in set(re.findall(r"realme-buds (?:--(?:profile-file|profile|addr|channel|transport|service-uuid) \S+ |--(?:trace|sim) )*([a-z][a-z-]+)", text)):
        if command in ("is", "says", "can", "and"):
            continue
        try:
            parser.parse_args([command, "--help"])
        except SystemExit as e:
            assert e.code == 0, f"the guide mentions 'realme-buds {command}' which the CLI rejects"


@pytest.mark.parametrize("name", ["README.md"])
def test_readme_mentions_the_advanced_tools_and_profiles(name):
    text = (DOCS.parent / name).read_text(encoding="utf-8")
    assert "profile" in text and "Advanced" in text


def test_models_doc_is_up_to_date():
    import importlib.util
    import sys

    path = DOCS.parent / "scripts" / "make_models_doc.py"
    spec = importlib.util.spec_from_file_location("make_models_doc", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["make_models_doc"] = mod
    spec.loader.exec_module(mod)
    assert (DOCS / "MODELS.md").read_text(encoding="utf-8") == mod.render(), "docs/MODELS.md is stale: run scripts/make_models_doc.py"


def test_license_and_notice_exist_and_name_the_data_source():
    assert "GNU GENERAL PUBLIC LICENSE" in (DOCS.parent / "LICENSE").read_text(encoding="utf-8")
    notice = (DOCS.parent / "NOTICE.md").read_text(encoding="utf-8")
    assert "QuickBuds" in notice and "GPL" in notice
