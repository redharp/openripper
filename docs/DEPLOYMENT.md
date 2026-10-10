# Deployment

OpenRipper is a Linux Docker appliance. The container needs:

- the optical drive's `/dev/sr*` block device;
- the matching `/dev/sg*` SCSI generic device used by MakeMKV;
- a writable host directory or mounted network share for `/media/library`;
- persistent `/config` storage for MakeMKV settings and data;
- PostgreSQL 17 for history, device state, jobs, and events;
- a valid MakeMKV beta or paid key when MakeMKV requires one.

## 1. Find the drive pairs

Install `lsscsi` on the Docker host and run:

```sh
lsscsi -g
```

Example:

```text
[5:0:0:0] cd/dvd PIONEER BD-RW BDR-212U 1.02 /dev/sr0 /dev/sg1
```

The default `compose.yaml` uses `privileged: true` because it is the most
reliable option for a multi-drive appliance and lets hot-added drives appear
without recreating the container. It bind-mounts `/dev` and `/run/udev` so
device nodes and hotplug events stay current. That grants broad host device
access.

For a fixed, tighter device list:

```sh
docker compose -f compose.yaml -f compose.devices.example.yaml up -d --build
```

Edit `compose.devices.example.yaml` first so every `/dev/sr*` device is paired
with the correct `/dev/sg*` device. This override removes the broad `/dev:/dev`
bind and falls back to periodic MakeMKV reconciliation for the listed devices.
It requires Docker Compose 2.24.4 or newer for `!override` support.

## 2. Mount the destination on the host

Mount NFS or SMB on the Docker host, not inside the container. Confirm the
mount is writable before starting OpenRipper:

```sh
touch /path/to/media/.openripper-write-test
rm /path/to/media/.openripper-write-test
```

Point `OPENRIPPER_LIBRARY_HOST_PATH` at that host path. Staging is kept below
the library root at `.openripper-staging`, so publishing is normally an atomic
rename on the same filesystem.

Set `OPENRIPPER_MOVIE_ROOT` and `OPENRIPPER_TV_ROOT` to the movie and
television directories inside that mounted filesystem. They may use different
names or casing, but should remain on the same filesystem as the staging root.

Use a writable export dedicated to your intended destination. Do not use a
read-only recovery or backup share. Verify the mount is present before starting
the app so a missing network mount cannot send files to the host's local disk.

## 3. Configure PostgreSQL and start

```sh
cp .env.example .env
```

Set at least:

```dotenv
OPENRIPPER_LIBRARY_HOST_PATH=/path/to/writable/media
OPENRIPPER_MOVIE_ROOT=/media/library/movies
OPENRIPPER_TV_ROOT=/media/library/tv
OPENRIPPER_MAKEMKV_KEY=your-current-key
OPENRIPPER_TMDB_TOKEN=your-optional-tmdb-v4-read-token
POSTGRES_PASSWORD=replace-this-with-a-long-random-password
```

Then:

```sh
docker compose up -d --build
docker compose logs -f openripper
```

Open `http://DOCKER-HOST:8080`.

The application container waits for PostgreSQL health and applies Alembic
migrations before the API starts.

The default `OPENRIPPER_OUTPUT_MODE=disc` needs no TMDB token or naming review.
It saves selected titles into a unique folder below the destination, then ejects
the disc. Use `OPENRIPPER_OUTPUT_MODE=library` for metadata-gated publishing;
that mode requests title/year confirmation when no confident match is available.
Dashboard preferences in `/config/openripper.json` override these defaults.

## 4. Reverse proxy and access

OpenRipper currently has no built-in user accounts. Keep port 8080 on a trusted
LAN, or put it behind the existing authenticated/access-listed reverse proxy.
Do not expose it directly to the public internet.

## 5. Update MakeMKV

The MakeMKV runtime comes from the tag-and-digest-pinned
`jlesage/makemkv` base in `Dockerfile`. To upgrade, review a published image,
replace both its tag and digest in `Dockerfile`, then rebuild:

```sh
docker compose build --no-cache
docker compose up -d
```

MakeMKV beta builds are time-limited. A paid key avoids beta expiration; a
current beta key also works while valid.

## 6. Drive compatibility

When a drive appears, OpenRipper reads its details through MakeMKV once. This
is read-only and never changes the drive. The dashboard shows one of:

- **4K UHD ready**: a known UHD-capable model with LibreDrive enabled.
- **UHD needs firmware**: a UHD-capable model where LibreDrive isn't enabled on
  its current firmware. Blu-ray and DVD still rip normally.
- **Blu-ray & DVD**: any other drive.

OpenRipper does not flash firmware. Set `OPENRIPPER_COMPATIBILITY_CHECK=false`
to skip the check.

## 7. Simulation mode

To exercise the entire UI without optical hardware:

```sh
docker compose -f compose.yaml -f compose.simulate.yaml up -d --build
```

Simulation writes a tiny placeholder file, never a real video.
The Docker demo opens on port 8081 and uses separate configuration, output
folders, and a separate PostgreSQL volume.
