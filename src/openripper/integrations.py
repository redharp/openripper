"""Durable handoff stages stored with job events, independent of rip success."""

import asyncio
import hashlib
from collections import Counter
from pathlib import Path

from .arr import ArrClient, ArrConfig, ArrError, normalized_title


def handoff_state(job: dict) -> dict:
    for event in job.get("events", []):
        if event.get("details", {}).get("integration"):
            return event["details"]
    return {}


class Handoffs:
    def __init__(self, settings, database, broadcast):
        self.settings, self.database, self.broadcast = settings, database, broadcast
        self.tasks: dict[str, asyncio.Task] = {}

    def config(self, provider):
        return ArrConfig(**self.settings.integrations.get(provider, {}))

    def client(self, provider):
        config = self.config(provider)
        if not config.enabled:
            raise ArrError(f"Enable {provider.title()} in settings first")
        return ArrClient(provider, config)

    async def record(self, job_id, state, message):
        state["message"] = message
        self.database.add_event(
            f"Library handoff: {message}",
            job_id=job_id,
            level="warning"
            if state["status"] in {"failed", "uncertain", "needs_match"}
            else "info",
            details=state.copy(),
        )
        await self.broadcast()

    async def start(self, job_id, provider, target_id=None, selections=None):
        if self.settings.simulate:
            raise ArrError("Simulation files cannot be sent to a media library")
        job = self.database.job_detail(job_id)
        if not job or job["status"] != "complete":
            raise ArrError("Only completed rips can be sent for naming")
        previous = handoff_state(job)
        if job_id in self.tasks or previous.get("status") in {
            "running",
            "waiting",
            "complete",
            "uncertain",
        }:
            raise ArrError(
                "This handoff is already running, finished, or needs its server activity checked"
            )
        client = self.client(provider)
        state = {
            "integration": provider,
            "status": "running",
            "phase": "prepare",
            "target_id": target_id,
            "selections": selections,
            "server": hashlib.sha256(client.config.url.encode()).hexdigest(),
        }
        await self.record(job_id, state, "Matching finished files")
        self.launch(job_id, state)

    def launch(self, job_id, state):
        task = asyncio.create_task(self.run(job_id, state))
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))

    async def stop(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def resume(self):
        rows = self.database.fetchall(
            "SELECT DISTINCT job_id FROM events WHERE message LIKE 'Library handoff:%'"
        )
        for row in rows:
            job = self.database.job_detail(row["job_id"])
            state = handoff_state(job or {})
            if state.get("status") in {"running", "waiting"}:
                if state.get("phase") == "sending":
                    state["status"] = "uncertain"
                    await self.record(
                        row["job_id"],
                        state,
                        "Restarted during submission. Check the server activity "
                        "before any manual import",
                    )
                else:
                    self.launch(row["job_id"], state)

    async def check(self, job_id):
        state = handoff_state(self.database.job_detail(job_id) or {})
        if state.get("status") != "waiting" or job_id in self.tasks:
            raise ArrError("This job has no paused naming status check")
        self.launch(job_id, state)

    async def auto(self, job_id):
        if self.settings.simulate:
            return
        job = self.database.job_detail(job_id)
        provider = "sonarr" if job["media_type"] == "tv" else "radarr"
        if self.config(provider).enabled:
            try:
                await self.start(job_id, provider)
            except ArrError:
                # A saved rip stays complete even if its independent handoff cannot start.
                self.database.add_event(
                    "Naming handoff could not start; use Process names",
                    job_id=job_id,
                    level="warning",
                )

    async def files(self, client, target_id):
        route = "moviefile" if client.provider == "radarr" else "episodefile"
        result = await client.request("GET", route, params={f"{client.kind}Id": target_id})
        if not isinstance(result, list):
            raise ArrError("The server returned an invalid file list")
        return result

    async def send(self, job_id, state, client, command, next_phase):
        state["phase"] = "sending"
        await self.record(job_id, state, "Submitting naming work to the server")
        command_id = await client.command(command)
        state.update(command_id=command_id, phase=next_phase)
        await self.record(
            job_id,
            state,
            "Importing files" if next_phase == "import" else "Applying the server's naming rules",
        )

    async def run(self, job_id, state):
        try:
            if state["status"] == "waiting":
                state["status"] = "running"
                await self.record(job_id, state, "Checking the existing naming task")
            client = self.client(state["integration"])
            if hashlib.sha256(client.config.url.encode()).hexdigest() != state["server"]:
                raise ArrError(
                    "Server URL changed. Restore the original server to check this handoff"
                )
            job = self.database.job_detail(job_id)
            if state["phase"] == "prepare":
                if not state["target_id"]:
                    matches = [
                        m
                        for m in await client.library()
                        if normalized_title(m.get("title", "")) == normalized_title(job["title"])
                        and (not job["year"] or m.get("year") == job["year"])
                    ]
                    if len(matches) != 1 or client.provider == "sonarr":
                        state["status"] = "needs_match"
                        await self.record(
                            job_id,
                            state,
                            "Choose the existing movie or show and confirm its "
                            "files in Process names",
                        )
                        return
                    state["target_id"] = matches[0]["id"]
                    selected = [t for t in job["titles"] if t["selected"] and t["ripped_path"]]
                    if not selected:
                        raise ArrError("No finished files were found")
                    main = max(selected, key=lambda t: t["duration_seconds"])
                    state["selections"] = [{"title_id": main["id"], "episode_ids": []}]
                plan = await client.prepare(job, state["target_id"], state["selections"])
                state["title"] = plan["title"]
                selected_ids = {s["title_id"] for s in state["selections"]}
                state["sizes"] = [
                    Path(t["ripped_path"]).stat().st_size
                    for t in job["titles"]
                    if t["id"] in selected_ids
                ]
                state["before_ids"] = [
                    f["id"] for f in await self.files(client, state["target_id"])
                ]
                await self.send(
                    job_id,
                    state,
                    client,
                    {"name": "ManualImport", "importMode": "copy", "files": plan["files"]},
                    "import",
                )
            while True:
                try:
                    command = await client.request("GET", f"command/{state['command_id']}")
                except ArrError:
                    state["status"] = "waiting"
                    await self.record(
                        job_id,
                        state,
                        "Cannot check the server. Restore the connection, then "
                        "check naming status; no import will be sent again",
                    )
                    return
                status = command.get("status")
                if status in {"failed", "aborted", "cancelled", "orphaned"}:
                    raise ArrError(
                        "The server could not finish the naming task. Check its "
                        "activity and import "
                        "warnings"
                    )
                if status != "completed":
                    await asyncio.sleep(5)
                    continue
                try:
                    files = await self.files(client, state["target_id"])
                except ArrError:
                    state["status"] = "waiting"
                    await self.record(
                        job_id,
                        state,
                        "Cannot verify library files. Restore the connection, "
                        "then check naming status",
                    )
                    return
                if state["phase"] == "import":
                    imported = [f for f in files if f["id"] not in state["before_ids"]]
                    if len(imported) != len(state["selections"]):
                        raise ArrError(
                            "Import command finished, but the expected new files "
                            "were not confirmed. "
                            "Check the server's import results"
                        )
                    if Counter(f.get("size") for f in imported) != Counter(state["sizes"]):
                        raise ArrError(
                            "New library files do not match the copied file sizes. "
                            "Check the server "
                            "before renaming"
                        )
                    if client.provider == "sonarr":
                        episodes = await client.request(
                            "GET", "episode", params={"seriesId": state["target_id"]}
                        )
                        expected = {e for s in state["selections"] for e in s["episode_ids"]}
                        linked = {
                            e["episodeFileId"]
                            for e in episodes
                            if e["id"] in expected and e.get("episodeFileId")
                        }
                        if linked != {f["id"] for f in imported}:
                            raise ArrError(
                                "Imported episode assignments could not be confirmed; check Sonarr"
                            )
                    state["file_ids"] = [f["id"] for f in imported]
                    await self.send(
                        job_id,
                        state,
                        client,
                        {
                            "name": "RenameFiles",
                            f"{client.kind}Id": state["target_id"],
                            "files": state["file_ids"],
                        },
                        "rename",
                    )
                    continue
                outputs = [f.get("path", "") for f in files if f["id"] in state["file_ids"]]
                if len(outputs) != len(state["file_ids"]) or not all(outputs):
                    raise ArrError(
                        "The server finished renaming but did not confirm all final paths"
                    )
                state.update(status="complete", outputs=outputs)
                await self.record(job_id, state, f"Names processed by {client.provider.title()}")
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Transport errors during POST may mean the command was accepted: don't auto-retry.
            state["status"] = "uncertain" if state.get("phase") == "sending" else "failed"
            message = (
                str(exc)
                if isinstance(exc, ArrError)
                else "Naming handoff failed. Check the integration settings and server activity"
            )
            await self.record(job_id, state, message)
