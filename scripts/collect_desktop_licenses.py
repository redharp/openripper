"""Preserve installed dependency notices beside the portable application."""

import importlib.metadata
import json
import shutil
import sys
from pathlib import Path


def main():
    target = Path("dist/desktop/OpenRipper/licenses")
    target.mkdir(parents=True, exist_ok=True)
    manifest = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"]
        manifest.append({"name": name, "version": distribution.version})
        destination = target / name
        destination.mkdir(exist_ok=True)
        (destination / "METADATA.txt").write_text(
            distribution.read_text("METADATA") or "", encoding="utf-8"
        )
        for file in distribution.files or []:
            if any(word in file.name.lower() for word in ("license", "copying", "notice")):
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    # Keep separate names if upstream carries multiple license files.
                    safe_name = "__".join(part for part in file.parts if part not in {"..", "."})
                    shutil.copy2(source, destination / safe_name)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, target / "Python-LICENSE.txt")
    (target / "build-dependencies.json").write_text(
        json.dumps(sorted(manifest, key=lambda item: item["name"]), indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
