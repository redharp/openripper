import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_autorip import finish, make_service

from openripper.api import create_app
from openripper.arr import ArrClient, ArrConfig, ArrError, mapped_path
from openripper.config import Settings
from openripper.integrations import Handoffs, handoff_state


def connection(**kwargs):
    return ArrConfig(enabled=True, url="http://arr.test/base", api_key="test-only-key", **kwargs)


@pytest.mark.parametrize(
    "url", ["ftp://arr.test", "http://user:key@arr.test", "http://arr.test/?key=secret"]
)
def test_connection_rejects_embedded_credentials_and_wrong_schemes(url):
    with pytest.raises(ValidationError):
        ArrConfig(url=url)


def test_path_mapping_handles_unc_and_docker_without_prefix_collisions():
    config = connection(local_path=r"\\media-server\rips", remote_path="/data/rips")
    assert (
        mapped_path(r"\\media-server\rips\Movie\title.mkv", config) == "/data/rips/Movie/title.mkv"
    )
    with pytest.raises(ArrError):
        mapped_path(r"\\media-server\rips-other\title.mkv", config)
    with pytest.raises(ArrError):
        mapped_path(r"\\media-server\rips\..\private\title.mkv", config)
    with pytest.raises(ValidationError):
        connection(local_path="/rips")


def test_keys_are_private_preserved_on_normal_save_and_not_reused_for_new_url(
    tmp_path, monkeypatch
):
    settings = Settings(
        library_root=tmp_path / "rips",
        database_url=f"sqlite:///{tmp_path / 'db'}",
        preferences_path=tmp_path / "settings.json",
        auto_rip=False,
        simulate=True,
        udev_discovery=False,
        compatibility_check=False,
    )
    with TestClient(create_app(settings)) as client:
        values = connection().private()
        response = client.put("/api/integrations/radarr", json=values)
        assert response.status_code == 200
        assert "test-only-key" not in response.text
        public = response.json()
        assert public["key_configured"]
        assert "api_key" not in client.get("/api/integrations").text
        public.pop("key_configured")
        assert client.put("/api/integrations/radarr", json=public).status_code == 200
        prefs = client.get("/api/settings").json()
        assert client.put("/api/settings", json=prefs).status_code == 200
        assert (
            json.loads(settings.preferences_path.read_text())["integrations"]["radarr"]["api_key"]
            == "test-only-key"
        )
        assert (
            client.put(
                "/api/integrations/radarr", json={**public, "url": "http://other.test"}
            ).status_code
            == 422
        )
        assert settings.integrations["radarr"]["url"] == values["url"]
        monkeypatch.setenv("OPENRIPPER_PREFERENCES_PATH", str(settings.preferences_path))
        assert Settings.from_env().integrations["radarr"]["api_key"] == "test-only-key"
        assert (
            client.put(
                "/api/integrations/radarr", json={**public, "enabled": False, "clear_api_key": True}
            ).status_code
            == 200
        )
        assert not client.get("/api/integrations").json()["radarr"]["key_configured"]


@pytest.mark.asyncio
async def test_http_contract_auth_base_url_and_redacted_failures(monkeypatch):
    calls = []
    status = 200

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status, json={"appName": "Radarr", "version": "6.0", "error": "test-only-key"}
        )

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        "openripper.arr.httpx.AsyncClient",
        lambda **kw: factory(transport=httpx.MockTransport(respond), **kw),
    )
    client = ArrClient("radarr", connection())
    assert (await client.test())["version"] == "6.0"
    assert str(calls[0].url) == "http://arr.test/base/api/v3/system/status"
    assert calls[0].headers["X-Api-Key"] == "test-only-key"
    for code in (401, 302, 500):
        status = code
        with pytest.raises(ArrError) as error:
            await client.test()
        assert "test-only-key" not in str(error.value)


class ArrServer:
    """Both providers' documented v3 request/response shapes, with real source paths."""

    def __init__(self, job, provider="radarr"):
        self.job = job
        self.provider = provider
        self.kind = "movie" if provider == "radarr" else "series"
        self.commands = []
        self.imported = False
        self.renamed = False
        self.reject = False
        self.zero_imports = False
        self.fail_status = False
        self.fail_submit = False

    async def request(self, client, method, route, **kw):
        title = self.job["titles"][0]
        if route == self.kind:
            return [{"id": 7, "title": self.job["title"], "year": self.job["year"]}]
        if route == f"{self.kind}/7":
            return {"id": 7, "title": self.job["title"], "path": "/library/Movie"}
        if route == "manualimport" and method == "GET":
            return [
                {
                    "path": title["ripped_path"],
                    "quality": {"quality": {"id": 7}},
                    "languages": [{"id": 1}],
                    "rejections": [{"reason": "Unknown title"}],
                }
            ]
        if route == "manualimport" and method == "POST":
            return [
                {**f, "rejections": [{"reason": "Existing file"}] if self.reject else []}
                for f in kw["json"]
            ]
        if route == "episode":
            return [
                {
                    "id": 81,
                    "seasonNumber": 1,
                    "episodeNumber": 2,
                    "episodeFileId": 12 if self.imported else 0,
                }
            ]
        if route in {"moviefile", "episodefile"}:
            if not self.imported or self.zero_imports:
                return []
            return [
                {
                    "id": 12,
                    "size": len(b"test video contents"),
                    "path": "/library/Correct Name.mkv" if self.renamed else "/library/raw.mkv",
                }
            ]
        if route == "command":
            self.commands.append(kw["json"])
            if self.fail_submit:
                raise ArrError("Connection lost")
            self.imported = True
            if kw["json"]["name"] == "RenameFiles":
                self.renamed = True
            return {"id": len(self.commands)}
        if route.startswith("command/"):
            if self.fail_status:
                raise ArrError("Offline")
            return {"status": "completed"}
        raise AssertionError((method, route, kw))


async def completed_service(tmp_path):
    service = make_service(tmp_path)
    await service.poll_once()
    await finish(service)
    job = service.database.job_detail(service.database.overview()["history"][0]["id"])
    service.settings.simulate = False  # Fake backend already completed; requests stay mocked.
    service.settings.integrations = {p: connection().private() for p in ("radarr", "sonarr")}
    return service, job


def install_server(monkeypatch, server):
    async def request(client, method, route, **kw):
        return await server.request(client, method, route, **kw)

    monkeypatch.setattr(ArrClient, "request", request)


async def drain(service):
    await asyncio.gather(*list(service.handoffs.tasks.values()))
    await asyncio.sleep(0)


@pytest.mark.parametrize("provider", ["radarr", "sonarr"])
@pytest.mark.asyncio
async def test_import_then_rename_and_verify_final_paths(tmp_path, monkeypatch, provider):
    service, job = await completed_service(tmp_path)
    server = ArrServer(job, provider)
    install_server(monkeypatch, server)
    selections = [
        {"title_id": job["titles"][0]["id"], "episode_ids": [81] if provider == "sonarr" else []}
    ]
    await service.handoffs.start(job["id"], provider, 7, selections)
    await drain(service)
    final = service.database.job_detail(job["id"])
    assert final["status"] == "complete"
    assert handoff_state(final)["status"] == "complete"
    assert handoff_state(final)["outputs"] == ["/library/Correct Name.mkv"]
    assert [c["name"] for c in server.commands] == ["ManualImport", "RenameFiles"]
    assert server.commands[0]["importMode"] == "copy"
    assert server.commands[1]["files"] == [12]
    with pytest.raises(ArrError):
        await service.handoffs.start(job["id"], provider, 7, selections)


@pytest.mark.parametrize("failure", ["reject", "zero_imports", "fail_submit"])
@pytest.mark.asyncio
async def test_rejection_or_unconfirmed_import_never_reports_naming_success(
    tmp_path, monkeypatch, failure
):
    service, job = await completed_service(tmp_path)
    server = ArrServer(job)
    setattr(server, failure, True)
    install_server(monkeypatch, server)
    await service.handoffs.start(job["id"], "radarr", 7, [{"title_id": job["titles"][0]["id"]}])
    await drain(service)
    final = service.database.job_detail(job["id"])
    assert final["status"] == "complete"
    assert handoff_state(final)["status"] == ("uncertain" if failure == "fail_submit" else "failed")
    assert not any(c["name"] == "RenameFiles" for c in server.commands)


@pytest.mark.asyncio
async def test_paused_status_resumes_without_duplicate_import(tmp_path, monkeypatch):
    service, job = await completed_service(tmp_path)
    server = ArrServer(job)
    server.fail_status = True
    install_server(monkeypatch, server)
    await service.handoffs.start(job["id"], "radarr", 7, [{"title_id": job["titles"][0]["id"]}])
    await drain(service)
    assert handoff_state(service.database.job_detail(job["id"]))["status"] == "waiting"
    server.fail_status = False
    service.handoffs = Handoffs(service.settings, service.database, service.broadcast)
    await service.handoffs.resume()
    await drain(service)
    assert [c["name"] for c in server.commands] == ["ManualImport", "RenameFiles"]


@pytest.mark.asyncio
async def test_unknown_movie_and_tv_disc_require_explicit_matching(tmp_path, monkeypatch):
    service, job = await completed_service(tmp_path)
    server = ArrServer(job)
    install_server(monkeypatch, server)
    service.database.update_job(job["id"], title="Uncertain Disc")
    await service.handoffs.auto(job["id"])
    await drain(service)
    assert handoff_state(service.database.job_detail(job["id"]))["status"] == "needs_match"
    service.database.update_job(job["id"], media_type="tv")
    install_server(monkeypatch, ArrServer(job, "sonarr"))
    await service.handoffs.auto(job["id"])
    await drain(service)
    assert handoff_state(service.database.job_detail(job["id"]))["status"] == "needs_match"
    assert not server.commands


@pytest.mark.asyncio
async def test_sonarr_rejects_wrong_episode_assignment_before_import(tmp_path, monkeypatch):
    service, job = await completed_service(tmp_path)
    server = ArrServer(job, "sonarr")
    install_server(monkeypatch, server)
    with pytest.raises(ArrError, match="distinct episodes"):
        await ArrClient("sonarr", connection()).prepare(
            job, 7, [{"title_id": job["titles"][0]["id"], "episode_ids": [999]}]
        )
    assert not server.commands


@pytest.mark.asyncio
@pytest.mark.parametrize("episode, expected", [(2, "complete"), (99, "failed")])
async def test_sonarr_reuses_confirmed_matching_without_guessing(
    tmp_path, monkeypatch, episode, expected
):
    service, job = await completed_service(tmp_path)
    service.database.update_job(job["id"], media_type="tv")
    service.database.add_event(
        "Episode assignments confirmed",
        job_id=job["id"],
        details={"assignments": [dict(title_id=job["titles"][0]["id"], season=1, episode=episode)]},
    )
    server = ArrServer(job, "sonarr")
    install_server(monkeypatch, server)
    await service.handoffs.auto(job["id"])
    await drain(service)
    result = service.database.job_detail(job["id"])
    assert handoff_state(result)["status"] == expected
    assert result["status"] == "complete"
    assert len(server.commands) == (2 if expected == "complete" else 0)
