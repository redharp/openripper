# Using OpenRipper

## API

Interactive API documentation is available at `/api/docs`. Core endpoints:

- `GET /api/overview`
- `GET /api/settings`
- `PUT /api/settings`
- `GET /api/jobs/{id}`
- `POST /api/jobs/{id}/metadata`
- `POST /api/jobs/{id}/retry`
- `POST /api/jobs/{id}/cancel`
- `POST /api/jobs/{id}/start` (loaded disc awaiting a manual start)
- `POST /api/jobs/{id}/dismiss` (hide one failure; retain files)
- `POST /api/jobs/failed/clear` (hide all failures; retain files)
- `GET /api/handbrake/presets`
- `POST /api/drives/{id}/rip`
- `POST /api/drives/{id}/eject`
- `POST /api/drives/{id}/compatibility`
- `WS /api/ws`

## Configuration

Server defaults use environment variables documented in [`.env.example`](../.env.example).
Dashboard preferences are saved in `config/openripper.json` (Docker:
`/config/openripper.json`) and override the corresponding environment defaults.
Set `OPENRIPPER_PREFERENCES_PATH` to use a different preferences file. In Docker,
the dashboard destination is a path **inside the container**; configure the host
directory or mounted share with `OPENRIPPER_LIBRARY_HOST_PATH`.

The **Settings** page exposes concurrent jobs (scans, rips and optional encoding
share these slots), automatic insertion handling, title selection, output roots,
filename formats, per-drive destinations, audio/subtitle languages, bonus titles,
poster artwork and optional HandBrake encoding. Advanced preferences are stored
in the JSON preferences file rather than environment variables. Concurrent jobs
and automatic ripping can change while work is active; destination and content
settings are locked until active jobs and pending reviews are resolved.

With automatic ripping off, inserted discs appear under **Ready to start**.
Media type defaults to **Auto**; check the destination and press **Start**.
Use **Movie** or **TV show** only to override detection.
They do not scan or rip until started, and still respect the slot limit. Removing
the disc removes its waiting entry. Waiting entries survive a service restart.
Explicit season markers (such as `EXAMPLE_SHOW_S1_D1`), episode labels and episode
running-time clusters help identify TV. When a TMDB token is configured, lookup
considers both movies and TV in either output mode; ambiguous matches are not
treated as confirmed metadata. Movie and TV folders now apply in both output
modes, including disc-folder mode. Per-drive roots contain Movies and TV folders.
Staging for new jobs stays alongside the selected destination.

Disc-folder mode defaults to `{disc}`: the disc name with filesystem-unsafe
characters removed, without a job-ID suffix. Saved settings using the former
`{disc} - {job_id}` default upgrade automatically; custom templates are preserved.
Movie output filenames from MakeMKV stay unchanged in this mode. If a destination folder
already exists, the new rip stays in staging and reports a conflict instead of
overwriting it. This change does not rename previously completed folders.

TV rips always wait at **Needs episode matching**, in either output mode.
Choose **Match episodes**, confirm the show, and assign every selected file to
its actual season and episode. Search existing Sonarr shows to choose episode
names from its catalog, or enter the details manually. A file containing several
consecutive episodes can use the optional **Through** field. Extras can be
unselected and remain in staging. OpenRipper never infers broadcast order from
disc track order or runtime.

**Publish matched episodes** creates `Show/Season NN/Show - SxxExx.mkv` using
your TV filename template. Missing assignments, duplicate episodes and existing
output files block publication. Closing the matching screen leaves the rip held;
submitted assignments persist if publication fails. An enabled Sonarr handoff
reuses those assignments when the show and episodes match its catalog uniquely. The disc can be ejected and
the drive reused while a rip waits for matching.

New TV jobs stage in `.openripper-staging` beside the configured TV folder,
including per-drive destinations. Give OpenRipper write access to that parent,
and mount it persistently in Docker. Point media scanners at the movie and TV
folders, not their shared parent. Same-filesystem publication is a rename;
cross-filesystem publication copies to a temporary non-video filename first.
Previously completed raw files are not automatically renamed or reclassified.

Use **Clear** on a failure or **Clear failed rips** above the list to hide entries.
This preserves partial files and diagnostic records; it never deletes media.

Queued jobs and drives have a **Rip now** button for starting that one queued
job above the concurrency limit. The saved slot limit stays unchanged; other
queued jobs keep waiting until capacity is available. The queue shows measured
write activity and how recently data arrived, distinguishing a waiting drive
from a dashboard that has stopped receiving updates.

Title preview is optional. When enabled, each disc scans and then waits for a
selection/naming review before writing video. Smart selection also pauses for
review when selected titles share a runtime and size but lack enough segment
information to rule out alternate versions. The unattended default keeps all
main-feature audio/subtitle tracks and excludes bonus titles. Posters require a
server-side TMDB token and a confident title/year match; a missing poster never
fails an otherwise successful rip. Browser alerts offer ding, muted, or off and
only operate while the dashboard is open.

HandBrake is disabled by default and must be installed separately on the ripper
host. Save its CLI executable in Settings, then use **Refresh presets** to load
the grouped preset dropdown from that installation. A missing CLI or failed
refresh preserves the saved preset. Encoding
produces MKV files; the preset controls video quality/resolution, and unsupported
audio may be converted to AAC. Originals remain on failures. With **Keep the
original MKV** enabled, disc mode saves both versions; library mode leaves the
original in staging. Successful encoding currently checks exit status and a
nonempty output, not a full playback verification. A separate encoding queue,
verified recovery, webhooks, and box-set sessions are tracked in
[the roadmap](ROADMAP.md).

Important knobs:

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENRIPPER_LIBRARY_ROOT` | `/media/library` | Base storage root; Movies and TV subfolders are the defaults |
| `OPENRIPPER_OUTPUT_MODE` | `disc` | Unattended disc folders, or `library` for metadata-gated naming |
| `OPENRIPPER_AUTO_RIP` | `true` | Start on insertion; when off, loaded discs wait for Start |
| `OPENRIPPER_MOVIE_ROOT` | `/media/library/Movies` | Movie destination in both output modes |
| `OPENRIPPER_TV_ROOT` | `/media/library/TV` | TV destination in both output modes |
| `OPENRIPPER_DATABASE_URL` | PostgreSQL DSN | Durable app state |
| `OPENRIPPER_RIP_MODE` | `smart` | `smart`, `main_feature`, or `all` |
| `OPENRIPPER_MIN_TITLE_SECONDS` | `1200` | Ignore short menus/trailers in automatic selection |
| `OPENRIPPER_AUTO_PUBLISH_CONFIDENCE` | `0.88` | Metadata threshold for unattended publishing |
| `OPENRIPPER_TMDB_TOKEN` | empty | Optional TMDB v4 read token |
| `OPENRIPPER_MAX_CONCURRENT_RIPS` | `2` | Shared scan/rip/encoding slot limit |
| `OPENRIPPER_UDEV_DISCOVERY` | `true` | Trigger immediate discovery on Linux hotplug events |
| `OPENRIPPER_COMPATIBILITY_CHECK` | `true` | Read-only check showing whether each drive can rip 4K UHD |
| `OPENRIPPER_SIMULATE` | `false` | Use two fake drives and a tiny fake rip |

## Sonarr and Radarr naming

Open **Settings → Sonarr & Radarr** to set each server URL and API key. Use its
base URL (including a reverse-proxy URL base, without `/api/v3`), then **Test
connection** and save. Each integration is off by default. Keys stay in the
ignored local configuration and are never returned by the settings API.
`OPENRIPPER_RADARR_URL`, `OPENRIPPER_RADARR_API_KEY`, `OPENRIPPER_SONARR_URL`, and
`OPENRIPPER_SONARR_API_KEY` can supply connection values at startup; environment
values override saved values on restart. Enable the integration in Settings.

For older completed rips, use **History → Details → Process names**. Select the
existing movie or series and the files to process; TV requires explicit episode
assignments (multiple episodes per file are supported). Raw disc filenames can
be matched here. Movies with exactly one matching title/year already in Radarr
can be handed off automatically after ripping. Unknown or ambiguous matches
wait for your selection. This release does not create movies or shows in either
server, guess TV episode order, or import bonus features as movies.

OpenRipper uses the server's manual-import checks, imports in **copy** mode, then
requests **RenameFiles** for the newly imported files. Quality/duplicate-file
rejections are never forced through. The job shows naming status separately
from ripping, and displays managed file paths after import and rename complete.
Your original rip remains at its saved location; these integrations do not
rename that original disc folder or rewrite embedded MKV title tags. Enable
renaming and configure your preferred naming pattern in Sonarr/Radarr.
Copies may use extra space unless the server can hardlink them. An existing
managed file or rejected upgrade should be reviewed in that server's manual
import screen rather than repeatedly submitted from OpenRipper.

Both applications must see the finished files. If their mount paths differ, set
**Shared-folder path mapping**, for example `/media/rips` on OpenRipper to
`/data/rips` in the other container. Map a common root covering all drive
outputs, or change the mapping for a different output root. The integration
checks path boundaries and never substitutes a partial folder-name prefix.
The server must also have permission to read that shared folder and write its
managed library.

Known command IDs survive restart. A paused connection check resumes the same
command; an uncertain submission is flagged for checking in the server's
activity, rather than risking a duplicate import. Simulation output cannot be
sent to a media library. These integrations use the official
[Radarr v3 API](https://radarr.video/docs/api/) and
[Sonarr v3 API](https://sonarr.tv/docs/api/).

## Drive compatibility

Each drive gets one read-only check through MakeMKV when it appears. The
dashboard shows **4K UHD ready**, **UHD needs firmware** (LibreDrive isn't
enabled on its current firmware; Blu-ray and DVD still rip), or
**Blu-ray & DVD**. OpenRipper never flashes firmware.
Drives reported as needing firmware show a link to [trev.bot](https://trev.bot/)
to check flashing support. This is a compatibility referral, not a guarantee that
a particular hardware revision or firmware can be flashed. No drive identifiers
are sent in the link.

## Operational boundaries

- A disc is handled once per service session. Removing and reinserting it allows
  another rip. After a restart, automatic ripping may process an inserted disc
  again; disable automation before restarting if you want to review it first.
- Interrupted work is marked failed. Retry can reuse completed titles, but the
  incomplete title starts again; this is not byte-level resume. Cancel and clear
  preserve partial files.
- OpenRipper remuxes tracks through MakeMKV by default. Optional HandBrake
  encoding runs only when explicitly enabled in Settings.
- Disc-folder mode uses the physical label without claiming a metadata match.
  Library mode holds uncertain naming for review.
- TV episodes need explicit per-file matching. Sonarr supplies show and episode
  names; it cannot identify anonymous disc tracks from their runtime alone.
- Use OpenRipper only for media you are legally permitted to copy in your
  jurisdiction.
- MakeMKV is separate software with its own license and beta-key requirements.
- LibreDrive `Status: Enabled` is a capability report; a real disc open is the
  final proof that LibreDrive engaged and that a particular UHD disc is readable.

## License

OpenRipper is MIT licensed. The packaged MakeMKV runtime remains separate
software and is not covered by this license.

## Troubleshooting

- **MakeMKV activation required:** open MakeMKV on the host and activate it, then
  use **Retry** on the failed job. Failed scans can be retried without reinserting.
- **Destination unavailable:** verify the account running OpenRipper can write to
  the share. Container destinations must be mounted inside the container.
- **No drive detected:** verify MakeMKV sees the drive and the container has both
  its optical and matching SCSI generic device, where applicable.
- **Saved successfully, but tray stayed closed:** the completed output is retained;
  tray errors appear as a warning in the job events.
- **No new data / 0 B/s:** the app is connected but the output file has stopped
  growing. The ETA changes to **Waiting for data** after a minute. Inspect the job
  events for MakeMKV read errors; persistent errors can require checking the disc
  or drive. A fresh dashboard update does not imply new video data was written.
- **Repeated title names:** job details show each title index and source playlist.
  Equal names or runtimes alone do not prove duplicates. Review uncertain
  selections before starting, especially on episodic and multi-version discs.
