"""Verify release payloads include the UI and omit local/private artifacts."""

import tarfile
import zipfile
from pathlib import Path


def main() -> None:
    wheels = list(Path("dist").glob("*.whl"))
    sources = list(Path("dist").glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise SystemExit("Expected one wheel and one source distribution in dist/")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        for filename in ("index.html", "app.js", "styles.css"):
            if f"openripper/static/{filename}" not in names:
                raise SystemExit(f"Wheel missing dashboard asset: {filename}")
    with tarfile.open(sources[0]) as archive:
        members = archive.getnames()
        names.extend(members)
        for filename in ("Dockerfile", "alembic.ini", "README.md", "LICENSE"):
            if not any(name.endswith(f"/{filename}") for name in members):
                raise SystemExit(f"Source distribution missing {filename}")
    forbidden = {".git", ".agents", ".codex", ".venv", "data", "library", "config"}
    for name in names:
        path = Path(name)
        if forbidden.intersection(path.parts) or path.name in {".env", "HOMELAB.md"}:
            raise SystemExit(f"Local artifact included in release: {name}")
        if path.suffix in {".db", ".log", ".pem", ".key", ".mkv", ".iso"}:
            raise SystemExit(f"Private/binary artifact included in release: {name}")
    print("Release package contents verified")


if __name__ == "__main__":
    main()
