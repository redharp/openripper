from xml.etree import ElementTree as ET

import pytest

from openripper.output import render_name, track_profile, validate_template


@pytest.mark.parametrize("template", ["../{title}", "{title.__class__}", "{missing}", "{title!r}"])
def test_naming_rejects_paths_and_unsupported_expressions(template):
    with pytest.raises(ValueError):
        validate_template(template)


def test_naming_formats_episodes_and_sanitizes_source():
    assert (
        render_name("{title} - S{season:02d}E{episode:02d}", title="My:Show", season=2, episode=3)
        == "My - Show - S02E03"
    )
    validate_template("{disc}")
    assert (
        render_name("{disc}", disc="HARRY_POTTER_CHAMBER_SECRETS") == "HARRY_POTTER_CHAMBER_SECRETS"
    )


def test_track_selection_profile_does_not_depend_on_global_preferences(tmp_path):
    path = track_profile(
        tmp_path / "profile.xml", audio="all", subtitles="preferred", languages="eng,jpn"
    )
    output = ET.parse(path).find("trackSettings/output")
    assert output is not None
    selection = output.attrib["defaultSelection"]
    assert "+sel:all" in selection
    assert "+sel:subtitle&(eng|jpn)" in selection
    assert "$app_DefaultSelectionString" not in selection
