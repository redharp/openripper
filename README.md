# OpenRipper

Self-hosted DVD and Blu-ray ripping. Insert a disc and save it to your own storage,
or turn automation off and start each rip from the queue.

- Multiple drives, live progress and configurable concurrent jobs
- Separate movie/TV folders, per-drive destinations and title previews
- Audio, subtitles, extras and optional HandBrake presets
- Sonarr/Radarr imports and naming for existing library titles
- Windows desktop companion or Linux Docker deployment

## Docker

MakeMKV is included in the image. On a Linux host with your optical drive attached:

```sh
git clone https://github.com/redharp/openripper.git
cd openripper
cp .env.example .env
```

Set `OPENRIPPER_LIBRARY_HOST_PATH` and `POSTGRES_PASSWORD` in `.env`, then:

```sh
docker compose up -d --build
```

Open [localhost:8080](http://localhost:8080). The default Compose setup uses
privileged device access; see [deployment options](docs/DEPLOYMENT.md) for explicit
drive mappings and storage setup.

## Windows

Install Python 3.12+ and [MakeMKV](https://www.makemkv.com/download/).
Open MakeMKV once to activate your license or evaluation, then run in this folder:

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\run-windows.cmd
```

Open [localhost:8080](http://localhost:8080) and choose **Settings** to set your
movie/TV folders and automatic-ripping preference. With automation off, inserted
discs wait for **Start**. Use `run-local.cmd` for a hardware-free demo.

For the desktop companion, see [app packaging](docs/DESKTOP.md).

## Documentation

- [Settings, naming integrations and troubleshooting](docs/USAGE.md)
- [Deployment](docs/DEPLOYMENT.md) and [security](SECURITY.md)
- [Contributing](CONTRIBUTING.md) and [roadmap](docs/ROADMAP.md)

Alpha software. Use only with media you are permitted to copy. OpenRipper is
[MIT licensed](LICENSE); MakeMKV has its own license. See [third-party notices](THIRD_PARTY_NOTICES.md).
