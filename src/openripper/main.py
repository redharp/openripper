from __future__ import annotations

import uvicorn

from .config import _env


def run() -> None:
    uvicorn.run(
        "openripper.api:create_app",
        host="0.0.0.0",
        port=int(_env("OPENRIPPER_PORT", "8080")),
        factory=True,
        proxy_headers=True,
    )


if __name__ == "__main__":
    run()
