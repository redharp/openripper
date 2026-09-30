# OpenRipper

**Insert a disc. Save it to your own storage.**

OpenRipper is a self-hosted, MIT-licensed app that watches optical drives and
automatically rips inserted DVDs and Blu-ray video discs to your configured
destination. It runs on native Windows or a Linux Docker host. No hosted account
is required. MakeMKV is the separate ripping engine; audio CDs are not supported.

**Alpha software:** hardware compatibility varies. See [contributing](CONTRIBUTING.md),
[security and deployment boundaries](SECURITY.md), and [third-party notices](THIRD_PARTY_NOTICES.md).

The default **Disc folders** mode is unattended: each rip is written below the
destination in a hidden staging folder, then moved into its own disc-label folder
when all selected titles finish. Unique job IDs prevent collisions with earlier
rips. A disc left in the drive is processed once per service session; removing
and reinserting it allows another rip. Restarting processes an inserted disc again.

Choose **Configure** in the dashboard to save your destination, automatic-ripping
preference, title-selection mode, and auto-eject preference. The destination can
be a local directory or a writable mounted share, including a Windows UNC path.
Settings persist across restarts and cannot change during an active rip.

Optional **Media library** mode uses Plex/Jellyfin-compatible naming and holds
uncertain metadata for confirmation. Optional TMDB matching is used only in this mode.

## What works

- Multiple concurrently connected Blu-ray/DVD drives.
- Linux udev hotplug discovery reconciled against MakeMKV's own drive indexes,
  with polling as a fallback.
- Stable `/dev/sr*` targeting plus MakeMKV `--noscan` isolation, so each drive
  can scan or rip without probing and pausing the others.
- Smart title selection:
  - longest title for a normal movie disc;
  - similarly sized episode titles for an episodic disc;
  - configurable `main_feature`, `smart`, or `all` mode.
- Optional TMDB matching for high-confidence automatic movie naming.
- Plex/Jellyfin movie layout:

  ```text
  Movies/Dune Part Two (2024)/Dune Part Two (2024).mkv
  ```

- Plex/Jellyfin TV layout:

  ```text
  TV/Show Name (2024)/Season 01/Show Name - S01E01.mkv
  ```

- Plex edition tags and an `Extras` folder for secondary selected titles.
- PostgreSQL 17 history and state managed through SQLAlchemy 2 and Alembic
  migrations.
- A read-only compatibility badge per drive: 4K UHD ready, UHD needs
  firmware, or Blu-ray & DVD.
- Per-title details, progress, errors, retry, cancel, and guarded tray controls.
- Clickable live job inspector and event feed with resilient WebSocket
  reconnect and polling fallback.
- Collision protection: an existing library file is never overwritten.
- Disc fingerprinting and a duplicate-completion warning.
- Responsive real-time dashboard over WebSockets.
- Docker image layered on the pinned, packaged `jlesage/makemkv` appliance.
- Hardware-free simulation mode for development and demos.

## Quick start

### Windows with a local optical drive

Install Python 3.12+ and MakeMKV, then run from this folder:

Open MakeMKV once and activate a license or start its evaluation before the first
real rip. Unattended scans cannot answer MakeMKV's activation prompt.

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\run-windows.cmd
```

Open [localhost:8080](http://localhost:8080), choose **Configure**, and set the
destination. Insert a DVD or Blu-ray: ripping starts on the next poll (5 seconds
by default, plus spin-up). Windows uses SQLite and native MakeMKV without Docker.
Set `OPENRIPPER_MAKEMKV_BIN` if MakeMKV is installed elsewhere. Network shares use
the Windows account running the app; connect to the share first.

For a hardware-free demo, run `run-local.cmd`. Its database, preferences, and
tiny placeholder outputs are kept separately under `data/`.

### Linux with Docker

Linux + Docker:

```sh
cp .env.example .env
```

Set `OPENRIPPER_LIBRARY_HOST_PATH` and a strong `POSTGRES_PASSWORD` in `.env`,
then:

```sh
docker compose up -d --build
```

Open `http://localhost:8080`.

The default Compose file is privileged so MakeMKV can discover all current and
hot-added optical/SCSI devices. A fixed-device, least-privilege example is
included in [`compose.devices.example.yaml`](compose.devices.example.yaml).
Read [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) before deploying against a real
media share.

## Local development

Python 3.12+:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
mkdir -p data/demo-library
export OPENRIPPER_SIMULATE=true
export OPENRIPPER_LIBRARY_ROOT=./data/demo-library
export OPENRIPPER_DATABASE_URL=sqlite:///./data/demo.db
export OPENRIPPER_PREFERENCES_PATH=./data/demo-settings.json
openripper
```

On Windows PowerShell:

```powershell
.\run-local.cmd
```

Run checks:

```sh
pytest
ruff check src tests migrations scripts
```

## API

Interactive API documentation is available at `/api/docs`. Core endpoints:

- `GET /api/overview`
- `GET /api/settings`
- `PUT /api/settings`
- `GET /api/jobs/{id}`
- `POST /api/jobs/{id}/metadata`
- `POST /api/jobs/{id}/retry`
- `POST /api/jobs/{id}/cancel`
- `POST /api/drives/{id}/rip`
- `POST /api/drives/{id}/eject`
- `POST /api/drives/{id}/compatibility`
- `WS /api/ws`

## Configuration

All settings use environment variables documented in [`.env.example`](.env.example).
Dashboard preferences are saved in `config/openripper.json` (Docker:
`/config/openripper.json`) and override the corresponding environment defaults.
Set `OPENRIPPER_PREFERENCES_PATH` to use a different preferences file. In Docker,
the dashboard destination is a path **inside the container**; configure the host
directory or mounted share with `OPENRIPPER_LIBRARY_HOST_PATH`.

Important knobs:

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENRIPPER_LIBRARY_ROOT` | `/media/library` | Final library and same-filesystem staging root |
| `OPENRIPPER_OUTPUT_MODE` | `disc` | Unattended disc folders, or `library` for metadata-gated naming |
| `OPENRIPPER_AUTO_RIP` | `true` | Start on insertion; manual Rip now still works when disabled |
| `OPENRIPPER_MOVIE_ROOT` | `/media/library/Movies` | Plex/Jellyfin movie destination |
| `OPENRIPPER_TV_ROOT` | `/media/library/TV` | Plex/Jellyfin television destination |
| `OPENRIPPER_DATABASE_URL` | PostgreSQL DSN | Durable app state |
| `OPENRIPPER_RIP_MODE` | `smart` | `smart`, `main_feature`, or `all` |
| `OPENRIPPER_MIN_TITLE_SECONDS` | `1200` | Ignore short menus/trailers in automatic selection |
| `OPENRIPPER_AUTO_PUBLISH_CONFIDENCE` | `0.88` | Metadata threshold for unattended publishing |
| `OPENRIPPER_TMDB_TOKEN` | empty | Optional TMDB v4 read token |
| `OPENRIPPER_MAX_CONCURRENT_RIPS` | `2` | Concurrent drives allowed to rip |
| `OPENRIPPER_UDEV_DISCOVERY` | `true` | Trigger immediate discovery on Linux hotplug events |
| `OPENRIPPER_COMPATIBILITY_CHECK` | `true` | Read-only check showing whether each drive can rip 4K UHD |
| `OPENRIPPER_SIMULATE` | `false` | Use two fake drives and a tiny fake rip |

## Drive compatibility

Each drive gets one read-only check through MakeMKV when it appears. The
dashboard shows **4K UHD ready**, **UHD needs firmware** (LibreDrive isn't
enabled on its current firmware; Blu-ray and DVD still rip), or
**Blu-ray & DVD**. OpenRipper never flashes firmware.

## Operational boundaries

- OpenRipper remuxes tracks through MakeMKV; it does not transcode video.
- Disc-folder mode uses the physical label without claiming a metadata match.
  Library mode holds uncertain naming for review.
- TV episode ordering still needs a season/first-episode confirmation unless a
  future metadata provider can identify the physical disc unambiguously.
- Use OpenRipper only for media you are legally permitted to copy in your
  jurisdiction.
- MakeMKV is separate software with its own license and beta-key requirements.
- LibreDrive `Status: Enabled` is a capability report; a real disc open is the
  final proof that LibreDrive engaged and that a particular UHD disc is readable.

## License

OpenRipper is MIT licensed. The packaged MakeMKV runtime remains separate
software and is not covered by this license.

## Upgrading from Disc Goblin

The package, command, and Compose service are now `openripper`. Reinstall the
editable package after updating. Python settings still accept `DISC_GOBLIN_*`;
the new `OPENRIPPER_*` prefix takes precedence. Rename variables in Compose `.env`.
Keep your existing PostgreSQL database/user/password and reuse your old volume
with `OPENRIPPER_POSTGRES_VOLUME`. Do not delete the old volume. The Windows
launcher reuses `data/disc-goblin.db` when no new database exists. Stored paths
for existing staged jobs remain in the database.

## Design tooling

The optional [Impeccable](https://impeccable.style/) skill is development tooling,
not an app dependency. Install it locally with
`npx impeccable install --providers=codex --scope=project --no-hooks`.
The downloaded tooling is excluded from source distributions and Git.

## Troubleshooting

- **MakeMKV activation required:** open MakeMKV on the host and activate it, then
  use **Retry** on the failed job. Failed scans can be retried without reinserting.
- **Destination unavailable:** verify the account running OpenRipper can write to
  the share. Container destinations must be mounted inside the container.
- **No drive detected:** verify MakeMKV sees the drive and the container has both
  its optical and matching SCSI generic device, where applicable.
- **Saved successfully, but tray stayed closed:** the completed output is retained;
  tray errors appear as a warning in the job events.
