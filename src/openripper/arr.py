"""Sonarr/Radarr v3 clients. Never expose upstream error bodies or API keys."""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, SecretStr, model_validator

PROVIDERS = {"radarr": "movie", "sonarr": "series"}


class ArrError(ValueError):
    pass


class ArrConfig(BaseModel):
    enabled: bool = False
    url: str = Field(default="", max_length=1000)
    api_key: SecretStr = SecretStr("")
    local_path: str = Field(default="", max_length=1000)
    remote_path: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_connection(self):
        self.url = self.url.strip().rstrip("/")
        self.local_path = self.local_path.strip()
        self.remote_path = self.remote_path.strip()
        if self.url:
            try:
                parsed = urlsplit(self.url)
                valid = (
                    parsed.scheme in {"http", "https"}
                    and parsed.hostname
                    and not parsed.username
                    and not parsed.password
                    and not parsed.query
                    and not parsed.fragment
                )
                _ = parsed.port
            except ValueError:
                valid = False
            if not valid:
                raise ValueError(
                    "Use an http(s) server URL without credentials or query parameters"
                )
        if bool(self.local_path) != bool(self.remote_path):
            raise ValueError("Set both sides of the path mapping, or leave both blank")
        for value in (self.local_path, self.remote_path):
            if value and (
                not path_type(value)(value).is_absolute() or ".." in path_type(value)(value).parts
            ):
                raise ValueError("Path mappings must use absolute paths without parent traversal")
        if self.enabled and (not self.url or not self.api_key.get_secret_value()):
            raise ValueError(
                "A server URL and API key are required before enabling the integration"
            )
        return self

    def private(self) -> dict:
        return {**self.model_dump(), "api_key": self.api_key.get_secret_value()}

    def public(self) -> dict:
        return {
            **self.model_dump(exclude={"api_key"}),
            "key_configured": bool(self.api_key.get_secret_value()),
        }


def path_type(value: str):
    return PureWindowsPath if "\\" in value or re.match(r"^[A-Za-z]:", value) else PurePosixPath


def mapped_path(value: str, config: ArrConfig) -> str:
    if not config.local_path:
        return value
    source = path_type(config.local_path)
    path = source(value)
    if ".." in path.parts:
        raise ArrError("The saved file path contains parent traversal")
    try:
        relative = path.relative_to(source(config.local_path))
    except ValueError as exc:
        raise ArrError(
            "This rip is outside the configured path mapping. Update the mapping in settings"
        ) from exc
    return str(path_type(config.remote_path)(config.remote_path).joinpath(*relative.parts))


def same_path(left: str, right: str) -> bool:
    return path_type(left)(left) == path_type(right)(right)


def normalized_title(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold())


class ArrClient:
    def __init__(self, provider: str, config: ArrConfig):
        if provider not in PROVIDERS:
            raise ArrError("Choose Sonarr or Radarr")
        self.provider = provider
        self.kind = PROVIDERS[provider]
        self.config = config

    async def request(self, method: str, route: str, **kwargs):
        if not self.config.url or not self.config.api_key.get_secret_value():
            raise ArrError("Save a server URL and API key first")
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                response = await client.request(
                    method,
                    f"{self.config.url}/api/v3/{route}",
                    headers={"X-Api-Key": self.config.api_key.get_secret_value()},
                    **kwargs,
                )
                if response.status_code in {401, 403}:
                    raise ArrError("API key rejected. Check the key and server permissions")
                if response.is_redirect:
                    raise ArrError(
                        "Server redirected the request. Use its final URL including any URL base"
                    )
                if response.is_error:
                    raise ArrError(
                        f"Server returned HTTP {response.status_code}. Check its activity log"
                    )
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, ArrError):
                raise
            raise ArrError(
                "Could not reach the server or read its response. Check the URL, connection "
                "and server logs"
            ) from None

    async def test(self) -> dict:
        result = await self.request("GET", "system/status")
        if not isinstance(result, dict) or not isinstance(result.get("version"), str):
            raise ArrError("The URL did not return a Sonarr/Radarr status response")
        app = result.get("appName", self.provider)
        if str(app).casefold() != self.provider:
            raise ArrError("This URL points to the wrong application")
        return {"message": f"Connected to {self.provider.title()}", "version": result["version"]}

    async def library(self, query: str = "") -> list[dict]:
        result = await self.request("GET", self.kind)
        if not isinstance(result, list):
            raise ArrError("The server returned an invalid library response")
        query = normalized_title(query)
        return [item for item in result if query in normalized_title(item.get("title", ""))]

    async def prepare(self, job: dict, target_id: int, selections: list[dict]) -> dict:
        target = await self.request("GET", f"{self.kind}/{target_id}")
        if not isinstance(target, dict) or target.get("id") != target_id:
            raise ArrError("The selected movie or show is no longer in the library")
        if not selections:
            raise ArrError("Select at least one ripped file")
        if self.provider == "radarr" and len(selections) != 1:
            raise ArrError("Choose one main feature for Radarr. Extras stay in OpenRipper")
        titles = {t["id"]: t for t in job["titles"] if t.get("ripped_path")}
        if len({s["title_id"] for s in selections}) != len(selections):
            raise ArrError("Each file can only be selected once")
        candidates = await self.request(
            "GET",
            "manualimport",
            params={
                "folder": mapped_path(job["final_path"], self.config),
                "filterExistingFiles": "false",
            },
        )
        if not isinstance(candidates, list):
            raise ArrError("The server returned an invalid import preview")
        episodes = {}
        if self.provider == "sonarr":
            episodes = {
                e["id"]: e
                for e in await self.request("GET", "episode", params={"seriesId": target_id})
            }
        used_episodes = set()
        files = []
        for selection in selections:
            title = titles.get(selection["title_id"])
            if not title:
                raise ArrError("A selected file does not belong to this completed rip")
            path = mapped_path(title["ripped_path"], self.config)
            matches = [c for c in candidates if same_path(c.get("path", ""), path)]
            if len(matches) != 1:
                raise ArrError(
                    "The server cannot see a selected file. Check the path mapping and "
                    "shared-folder permissions"
                )
            candidate = matches[0]
            item = {
                key: candidate[key]
                for key in (
                    "path",
                    "folderName",
                    "quality",
                    "languages",
                    "releaseGroup",
                    "indexerFlags",
                )
                if key in candidate
            }
            item[f"{self.kind}Id"] = target_id
            if self.provider == "sonarr":
                ids = selection.get("episode_ids", [])
                if (
                    not ids
                    or len(set(ids)) != len(ids)
                    or any(e not in episodes or e in used_episodes for e in ids)
                ):
                    raise ArrError(
                        "Choose distinct episodes from this show for every selected file"
                    )
                seasons = {episodes[e]["seasonNumber"] for e in ids}
                if len(seasons) != 1:
                    raise ArrError("Episodes in one file must belong to the same season")
                item.update(episodeIds=ids, seasonNumber=seasons.pop())
                used_episodes.update(ids)
            files.append(item)
        # Re-evaluate against the explicitly chosen movie/episodes; never bypass rejections.
        checked = await self.request("POST", "manualimport", json=files)
        if not isinstance(checked, list) or len(checked) != len(files):
            raise ArrError("The server could not validate all selected files")
        for original, item in zip(files, checked, strict=True):
            if not same_path(original["path"], item.get("path", "")):
                raise ArrError("The server changed the import selection. Review it in the server")
            if item.get("rejections"):
                raise ArrError(
                    "The server rejected an import. Review quality, existing files and import "
                    "warnings in Sonarr/Radarr"
                )
            for key in ("quality", "languages", "releaseGroup", "indexerFlags", "releaseType"):
                if key in item:
                    original[key] = item[key]
        return {"target_id": target_id, "title": target.get("title", ""), "files": files}

    async def command(self, payload: dict) -> int:
        result = await self.request("POST", "command", json=payload)
        if not isinstance(result, dict) or not isinstance(result.get("id"), int):
            raise ArrError(
                "The server did not return a command ID. Check its activity before retrying"
            )
        return result["id"]
