import json

import pytest
from fastapi.testclient import TestClient

from openripper.api import create_app
from openripper.config import Settings


def test_settings_persist_and_invalid_destination_does_not_replace_them(tmp_path, monkeypatch):
    root = tmp_path / "library"
    settings = Settings(
        library_root=root,
        movie_root=root / "Movies",
        tv_root=root / "TV",
        database_url=f"sqlite:///{tmp_path / 'api.db'}",
        preferences_path=tmp_path / "preferences.json",
        poll_interval=999,
        auto_rip=False,
        compatibility_check=False,
        udev_discovery=False,
        simulate=True,
    )
    with TestClient(create_app(settings)) as client:
        values = client.get("/api/settings").json()
        values["library_root"] = str(tmp_path / "new destination")
        values["output_mode"] = "library"
        assert client.put("/api/settings", json=values).status_code == 200
        assert (
            json.loads(settings.preferences_path.read_text())["library_root"]
            == values["library_root"]
        )
        assert (
            client.put("/api/settings", json={**values, "library_root": "relative"}).status_code
            == 422
        )
        assert (
            client.put("/api/settings", json={**values, "output_mode": "invalid"}).status_code
            == 422
        )
        assert client.get("/api/settings").json() == values
    monkeypatch.setenv("OPENRIPPER_PREFERENCES_PATH", str(settings.preferences_path))
    loaded = Settings.from_env()
    assert loaded.library_root == tmp_path / "new destination"
    assert loaded.output_mode == "library"
    assert loaded.auto_rip is False


def test_advanced_settings_validate_and_persist_without_partial_updates(tmp_path, monkeypatch):
    settings = Settings(
        library_root=tmp_path / "library",
        movie_root=tmp_path / "Movies",
        tv_root=tmp_path / "TV",
        database_url=f"sqlite:///{tmp_path / 'api.db'}",
        preferences_path=tmp_path / "preferences.json",
        poll_interval=999,
        auto_rip=False,
        compatibility_check=False,
        udev_discovery=False,
        simulate=True,
    )
    with TestClient(create_app(settings)) as client:
        values = client.get("/api/settings").json()
        values.update(
            review_before_rip=True,
            subtitle_mode="preferred",
            preferred_languages="eng,jpn",
            movie_name_template="{title} ({year})",
            drive_paths={"offline-drive": str(tmp_path / "other")},
        )
        assert client.put("/api/settings", json=values).status_code == 200
        for bad in (
            {"movie_name_template": "../{title}"},
            {"tv_root": "relative"},
            {"preferred_languages": "English"},
            {"max_concurrent_rips": 0},
            {"disc_folder_template": "../{disc}"},
        ):
            assert client.put("/api/settings", json={**values, **bad}).status_code == 422
        assert client.get("/api/settings").json() == values
    monkeypatch.setenv("OPENRIPPER_PREFERENCES_PATH", str(settings.preferences_path))
    loaded = Settings.from_env()
    assert loaded.review_before_rip is True
    assert loaded.preferred_languages == "eng,jpn"
    assert loaded.drive_paths == values["drive_paths"]


def test_environment_sets_default_media_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENRIPPER_PREFERENCES_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setenv("OPENRIPPER_LIBRARY_ROOT", str(tmp_path / "library"))
    settings = Settings.from_env()
    assert settings.library_root == tmp_path / "library"
    assert settings.movie_root == tmp_path / "library" / "Movies"
    assert settings.tv_root == tmp_path / "library" / "TV"


def test_cannot_change_destination_during_rip(tmp_path):
    settings = Settings(
        library_root=tmp_path / "library",
        movie_root=tmp_path / "Movies",
        tv_root=tmp_path / "TV",
        database_url=f"sqlite:///{tmp_path / 'api.db'}",
        preferences_path=tmp_path / "preferences.json",
        poll_interval=999,
        auto_rip=False,
        compatibility_check=False,
        udev_discovery=False,
        simulate=True,
    )
    with TestClient(create_app(settings)) as client:
        client.post("/api/poll")
        assert client.post("/api/drives/drive-demo-pioneer/rip").status_code == 202
        values = client.get("/api/settings").json()
        values["library_root"] = str(tmp_path / "different")
        assert client.put("/api/settings", json=values).status_code == 409
        values = client.get("/api/settings").json()
        values["max_concurrent_rips"] = 4
        assert client.put("/api/settings", json=values).status_code == 200
        assert client.get("/api/overview").json()["runtime"]["max_concurrent_rips"] == 4


@pytest.mark.parametrize(
    "template, expected",
    [("{disc} - {job_id}", "{disc}"), ("{title} ({year})", "{title} ({year})")],
)
def test_existing_default_folder_template_upgrades_without_replacing_custom_names(
    tmp_path, monkeypatch, template, expected
):
    preferences = tmp_path / "settings.json"
    preferences.write_text(json.dumps({"disc_folder_template": template}))
    monkeypatch.setenv("OPENRIPPER_PREFERENCES_PATH", str(preferences))
    assert Settings.from_env().disc_folder_template == expected
