import json
import os
import time

import pytest
from websockets.sync.client import connect

from openripper.desktop import LocalServer, desktop_settings, is_openripper, read_json, server_url


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith("OPENRIPPER_"):
            monkeypatch.delenv(key)


def test_desktop_defaults_are_private_and_paused(tmp_path):
    settings = desktop_settings(tmp_path)
    assert not settings.auto_rip
    assert not settings.udev_discovery
    assert settings.preferences_path == tmp_path / "preferences.json"
    assert str(tmp_path.as_posix()) in settings.database_url
    assert "OPENRIPPER_AUTO_RIP" not in os.environ


def test_desktop_preserves_preferences_and_explicit_environment(tmp_path, monkeypatch):
    preferences = tmp_path / "custom.json"
    preferences.write_text(json.dumps({"auto_rip": False, "max_concurrent_rips": 4}))
    monkeypatch.setenv("OPENRIPPER_AUTO_RIP", "true")
    monkeypatch.setenv("OPENRIPPER_PREFERENCES_PATH", str(preferences))
    settings = desktop_settings(tmp_path)
    assert not settings.auto_rip
    assert settings.max_concurrent_rips == 4
    assert os.environ["OPENRIPPER_AUTO_RIP"] == "true"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://user:pass@host",
        "http://host/path",
        "http://host?token=x",
        "http://host#fragment",
        "http://host:wrong",
    ],
)
def test_remote_address_validation(url):
    with pytest.raises(ValueError):
        server_url(url)


def test_remote_address_accepts_origin():
    assert server_url("https://ripper.local/") == "https://ripper.local"


def test_bundled_server_lifecycle_isolated_from_real_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENRIPPER_AUTO_RIP", "true")
    monkeypatch.setenv("OPENRIPPER_DATABASE_URL", "sqlite:///do-not-use.db")
    settings = desktop_settings(tmp_path, simulate=True)
    server = LocalServer(settings, port=0)
    try:
        url = server.start()
        assert is_openripper(url)
        assert read_json(url + "/healthz")["simulation"]
        assert read_json(url + "/api/settings")["auto_rip"] is False
        assert all(
            j["status"] == "awaiting_start" for j in read_json(url + "/api/overview")["active_jobs"]
        )
        with pytest.raises(OSError):
            LocalServer(settings, port=server.socket.getsockname()[1])
    finally:
        server.stop()
    assert not server.thread.is_alive()
    assert not is_openripper(url)


def test_closed_dashboard_releases_live_connection_without_next_poll(tmp_path):
    settings = desktop_settings(tmp_path, simulate=True)
    settings.poll_interval = 999
    server = LocalServer(settings, port=0)
    try:
        url = server.start()
        with connect(url.replace("http:", "ws:") + "/api/ws") as ws:
            assert "active_jobs" in json.loads(ws.recv())
        deadline = time.monotonic() + 2
        while server.server.server_state.tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not server.server.server_state.tasks
        assert not server.server.config.app.state.service._subscribers
    finally:
        server.stop()
