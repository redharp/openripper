import json

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


def test_legacy_environment_is_supported_and_new_prefix_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENRIPPER_PREFERENCES_PATH", str(tmp_path / "absent.json"))
    monkeypatch.delenv("OPENRIPPER_LIBRARY_ROOT", raising=False)
    monkeypatch.setenv("DISC_GOBLIN_LIBRARY_ROOT", str(tmp_path / "old"))
    assert Settings.from_env().library_root == tmp_path / "old"
    monkeypatch.setenv("OPENRIPPER_LIBRARY_ROOT", str(tmp_path / "new"))
    assert Settings.from_env().library_root == tmp_path / "new"


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
        assert client.put("/api/settings", json=values).status_code == 409
