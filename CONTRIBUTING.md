# Contributing to OpenRipper

Bug reports, documentation improvements, and pull requests are welcome.
OpenRipper is an early-stage project. Keep changes focused and describe the
problem, the resulting behavior, and how you verified it.

## Development

Use Python 3.12 or newer:

```sh
python -m venv .venv
# Linux: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check src tests migrations
```

Use the hardware-free setup in the README. Tests use temporary SQLite databases
and fake drives; they must not require optical hardware, API keys, or a NAS.
For changes to the MakeMKV parser, add a minimal, sanitized robot-output fixture.
For job lifecycle changes, cover cancellation, failure, and duplicate insertion
where relevant. Schema changes require an Alembic migration.

Check JavaScript with `node --check src/openripper/static/app.js`. Verify UI
changes at desktop and mobile sizes, including keyboard use and failure states.
Build distributable packages with `python -m build` after installing `build`.

## Pull requests

1. Create a branch from `main`.
2. Implement a focused change and update the relevant documentation.
3. Run the tests and lint checks. Include hardware/software versions when the
   behavior depends on a real drive.
4. Open a pull request with a description and verification results.

Do not include credentials, MakeMKV keys, personal share paths, databases, logs
from private systems, media files, decrypted disc content, or firmware binaries.
Do not add firmware flashing: compatibility checks are intentionally read-only.
Report security concerns privately as described in [SECURITY.md](SECURITY.md).

Contributions are provided under the repository's MIT license. Be respectful,
constructive, and considerate when discussing issues and reviewing changes.
