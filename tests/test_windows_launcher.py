"""Exercise the real Windows launcher without starting the ripper or hardware."""

import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows batch launcher")


@pytest.fixture(scope="module")
def launcher_workspace(tmp_path_factory):
    root = tmp_path_factory.mktemp("launcher with spaces")
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    shutil.copyfile(Path(__file__).parents[1] / "run-windows.cmd", root / "run-windows.cmd")
    (root / "data").mkdir()
    module = root / "openripper"
    module.mkdir()
    (module / "__init__.py").write_text("")
    # Capture exactly what the launcher passes to Python, without importing
    # the application, touching SQLite, detecting drives, or opening a port.
    (module / "main.py").write_text(
        "import json,os\nprint('LAUNCH_ENV='+json.dumps({k:v for k,v in os.environ.items() "
        "if k.startswith('OPENRIPPER_')}))\n"
    )
    return root


@pytest.mark.parametrize(
    ("existing", "explicit", "expected"),
    [
        (["disc-goblin.db"], None, "disc-goblin.db"),
        (["disc-goblin.db", "openripper.db"], None, "openripper.db"),
        ([], None, "openripper.db"),
        (["disc-goblin.db"], "sqlite:///custom.db", "sqlite:///custom.db"),
    ],
)
def test_launcher_preserves_existing_history(launcher_workspace, existing, explicit, expected):
    root = launcher_workspace
    for name in ("disc-goblin.db", "openripper.db"):
        (root / "data" / name).unlink(missing_ok=True)
    for name in existing:
        (root / "data" / name).write_bytes(b"untouched database sentinel")
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENRIPPER_")}
    env["OPENRIPPER_MAKEMKV_BIN"] = sys.executable  # Existence check only.
    if explicit:
        env["OPENRIPPER_DATABASE_URL"] = explicit
    result = subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "run-windows.cmd"],
        cwd=root, env=env, capture_output=True, text=True, check=True, timeout=30,
    )
    line = next(x for x in result.stdout.splitlines() if x.startswith("LAUNCH_ENV="))
    actual = json.loads(line.removeprefix("LAUNCH_ENV="))["OPENRIPPER_DATABASE_URL"]
    assert actual == (explicit or "sqlite:///" + str(root / "data" / expected))
    assert sorted(p.name for p in (root / "data").iterdir()) == sorted(existing)
    for name in existing:
        assert (root / "data" / name).read_bytes() == b"untouched database sentinel"
