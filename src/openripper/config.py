from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    library_root: Path = Path("/media/library")
    movie_root: Path = Path("/media/library/Movies")
    tv_root: Path = Path("/media/library/TV")
    database_url: str = "postgresql+psycopg://openripper:openripper@localhost:5432/openripper"
    poll_interval: float = 8.0
    auto_rip: bool = True
    eject_on_success: bool = True
    max_concurrent_rips: int = 2
    min_title_seconds: int = 1200
    auto_publish_confidence: float = 0.88
    rip_mode: str = "smart"
    tmdb_token: str | None = None
    makemkv_bin: str = "makemkvcon"
    makemkv_key: str | None = None
    udev_discovery: bool = True
    compatibility_check: bool = True
    sdf_path: Path = Path("/opt/makemkv/appdata/sdf.bin")
    simulate: bool = False
    output_mode: str = "disc"
    preferences_path: Path = Path("config/openripper.json")
    drive_paths: dict[str, str] = field(default_factory=dict)
    disc_folder_template: str = "{disc}"
    movie_name_template: str = "{title}{year_suffix}{edition_suffix}"
    tv_name_template: str = "{title} - S{season:02d}E{episode:02d}{episode_suffix}"
    include_extras: bool = False
    review_before_rip: bool = False
    extras_min_seconds: int = 60
    subtitle_mode: str = "all"
    audio_mode: str = "all"
    preferred_languages: str = "eng"
    artwork_enabled: bool = False
    handbrake_enabled: bool = False
    handbrake_bin: str = "HandBrakeCLI"
    handbrake_preset: str = "Fast 1080p30"
    handbrake_keep_original: bool = True
    integrations: dict[str, dict] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # Upgrade the previous default, including saved preferences.
        if self.disc_folder_template == "{disc} - {job_id}":
            self.disc_folder_template = "{disc}"
        if self.output_mode not in {"disc", "library"}:
            raise ValueError("OPENRIPPER_OUTPUT_MODE must be disc or library")
        if self.rip_mode not in {"smart", "main_feature", "all"}:
            raise ValueError("OPENRIPPER_RIP_MODE must be smart, main_feature or all")
        if self.poll_interval < 1:
            raise ValueError("OPENRIPPER_POLL_INTERVAL must be at least 1 second")
        if not 1 <= self.max_concurrent_rips <= 16:
            raise ValueError("Concurrent rips must be between 1 and 16")

    def public_preferences(self) -> dict:
        return {
            "library_root": str(self.library_root.resolve()),
            "auto_rip": self.auto_rip,
            "eject_on_success": self.eject_on_success,
            "output_mode": self.output_mode,
            "rip_mode": self.rip_mode,
            "max_concurrent_rips": self.max_concurrent_rips,
            "movie_root": str(self.movie_root.resolve()),
            "tv_root": str(self.tv_root.resolve()),
            **{name: getattr(self, name) for name in ADVANCED_PREFERENCES},
        }

    @property
    def staging_root(self) -> Path:
        return self.library_root / ".openripper-staging"

    @classmethod
    def from_env(cls) -> Settings:
        library_root = Path(_env("OPENRIPPER_LIBRARY_ROOT", "/media/library")).expanduser()
        settings = cls(
            library_root=library_root,
            movie_root=Path(
                _env(
                    "OPENRIPPER_MOVIE_ROOT",
                    str(library_root / "Movies"),
                )
            ).expanduser(),
            tv_root=Path(
                _env(
                    "OPENRIPPER_TV_ROOT",
                    str(library_root / "TV"),
                )
            ).expanduser(),
            database_url=_env(
                "OPENRIPPER_DATABASE_URL",
                "postgresql+psycopg://openripper:openripper@localhost:5432/openripper",
            ),
            poll_interval=float(_env("OPENRIPPER_POLL_INTERVAL", "8")),
            auto_rip=_bool("OPENRIPPER_AUTO_RIP", True),
            eject_on_success=_bool("OPENRIPPER_EJECT_ON_SUCCESS", True),
            max_concurrent_rips=max(1, int(_env("OPENRIPPER_MAX_CONCURRENT_RIPS", "2"))),
            min_title_seconds=max(0, int(_env("OPENRIPPER_MIN_TITLE_SECONDS", "1200"))),
            auto_publish_confidence=min(
                1.0,
                max(
                    0.0,
                    float(_env("OPENRIPPER_AUTO_PUBLISH_CONFIDENCE", "0.88")),
                ),
            ),
            rip_mode=_env("OPENRIPPER_RIP_MODE", "smart").strip().lower(),
            tmdb_token=_env("OPENRIPPER_TMDB_TOKEN") or None,
            makemkv_bin=_env("OPENRIPPER_MAKEMKV_BIN", "makemkvcon"),
            makemkv_key=_env("OPENRIPPER_MAKEMKV_KEY") or None,
            udev_discovery=_bool("OPENRIPPER_UDEV_DISCOVERY", True),
            compatibility_check=_bool("OPENRIPPER_COMPATIBILITY_CHECK", True),
            sdf_path=Path(_env("OPENRIPPER_SDF_PATH", "/opt/makemkv/appdata/sdf.bin")).expanduser(),
            simulate=_bool("OPENRIPPER_SIMULATE", False),
            output_mode=_env("OPENRIPPER_OUTPUT_MODE", "disc"),
            preferences_path=Path(_env("OPENRIPPER_PREFERENCES_PATH", "config/openripper.json")),
        )
        if settings.preferences_path.is_file():
            preferences = json.loads(settings.preferences_path.read_text(encoding="utf-8"))
            root = preferences.get("library_root")
            if root:
                settings.library_root = Path(root).expanduser()
                settings.movie_root = Path(
                    preferences.get("movie_root", settings.library_root / "Movies")
                )
                settings.tv_root = Path(preferences.get("tv_root", settings.library_root / "TV"))
            for name in (
                "auto_rip",
                "eject_on_success",
                "output_mode",
                "rip_mode",
                "max_concurrent_rips",
            ) + ADVANCED_PREFERENCES:
                if name in preferences:
                    setattr(settings, name, preferences[name])
            settings.__post_init__()
            settings.integrations = preferences.get("integrations", {})
        from .arr import ArrConfig

        for provider in ("radarr", "sonarr"):
            values = dict(settings.integrations.get(provider, {}))
            for field_name in ("url", "api_key"):
                value = _env(f"OPENRIPPER_{provider.upper()}_{field_name.upper()}")
                if value is not None:
                    values[field_name] = value
            settings.integrations[provider] = ArrConfig(**values).private()
        return settings


ADVANCED_PREFERENCES = (
    "review_before_rip",
    "drive_paths",
    "disc_folder_template",
    "movie_name_template",
    "tv_name_template",
    "include_extras",
    "extras_min_seconds",
    "min_title_seconds",
    "subtitle_mode",
    "audio_mode",
    "preferred_languages",
    "artwork_enabled",
    "handbrake_enabled",
    "handbrake_bin",
    "handbrake_preset",
    "handbrake_keep_original",
)
