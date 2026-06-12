from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_CONFIG_PATH = Path("config.ini")
DEFAULT_SESSION_PATH = Path(".state/telegram_downloader")
DEFAULT_STATE_DB_PATH = Path(".state/download_state.sqlite3")

DEFAULT_RETRIES = 3
DEFAULT_WORKERS = 4
DEFAULT_PART_SIZE_KB = 512
DEFAULT_UI_REFRESH_SECONDS = 1.0
DEFAULT_STALL_TIMEOUT_SECONDS = 120

DEFAULT_BENCHMARK_SECONDS = 15
DEFAULT_BENCHMARK_SAMPLE_FILES = 3
DEFAULT_BENCHMARK_BLOCK_MIN = 128
DEFAULT_BENCHMARK_BLOCK_MAX = 512
DEFAULT_BENCHMARK_WORKER_MAX = 4

CATEGORY_LABELS = {
    "archives": "Archives",
    "audio": "Audio",
    "documents": "Documents",
    "images": "Images",
    "video": "Video",
    "voice": "Voice notes",
}

FILTER_PRESETS: dict[str, dict[str, set[str]]] = {
    "Everything": {
        "categories": set(CATEGORY_LABELS),
        "extensions": set(),
    },
    "Music packs": {
        "categories": {"archives", "audio"},
        "extensions": set(),
    },
    "Archives only": {
        "categories": {"archives"},
        "extensions": set(),
    },
    "Audio only": {
        "categories": {"audio"},
        "extensions": set(),
    },
    "Lossless only": {
        "categories": {"audio"},
        "extensions": {".flac", ".wav", ".aiff", ".alac"},
    },
    "Compressed audio": {
        "categories": {"audio"},
        "extensions": {".mp3", ".aac", ".m4a", ".ogg", ".opus"},
    },
    "Custom": {
        "categories": set(CATEGORY_LABELS),
        "extensions": set(),
    },
}


def default_destination_path() -> Path:
    """Return a sensible per-platform default download folder."""
    downloads_dir = Path.home() / "Downloads"
    if os.name == "nt":
        return downloads_dir / "Telegram Files"
    return downloads_dir / "telegram-files"


DEFAULT_DESTINATION = default_destination_path()


@dataclass(slots=True)
class AppSettings:
    api_id: int
    api_hash: str
    source: str
    destination: Path
    session_path: Path
    state_db_path: Path
    categories: set[str]
    extensions: set[str]
    filter_preset: str
    extract_archives: bool
    keep_archives: bool
    retries: int
    workers: int
    part_size_kb: int
    ui_refresh_seconds: float
    stall_timeout_seconds: int
    benchmark_source: str
    benchmark_seconds: int
    benchmark_sample_files: int
    benchmark_block_min_kb: int
    benchmark_block_max_kb: int
    benchmark_worker_max: int


def normalize_source(value: str) -> str:
    source = value.strip()
    if not source:
        return source

    if re.fullmatch(r"-?\d+", source):
        return source

    if "t.me" in source:
        parsed = urlparse(source)
        path = parsed.path.strip("/")
        if re.fullmatch(r"-?\d+", path):
            return path
        match = re.search(r"(-100\d+)", source)
        if match:
            return match.group(1)

    if "web.telegram.org" in source:
        parsed = urlparse(source)
        fragment = (parsed.fragment or "").strip()
        match = re.search(r"(-100\d+)", fragment or source)
        if match:
            return match.group(1)

    return source


def normalize_extension(value: str) -> str:
    cleaned = value.strip().lower()
    if not cleaned:
        return ""
    if not cleaned.startswith("."):
        cleaned = f".{cleaned}"
    return cleaned


def normalize_extensions(raw_value: str) -> set[str]:
    if not raw_value.strip():
        return set()
    pieces = re.split(r"[\s,;]+", raw_value.strip())
    return {
        extension
        for piece in pieces
        if (extension := normalize_extension(piece))
    }


def normalize_categories(raw_value: str) -> set[str]:
    if not raw_value.strip():
        return set(CATEGORY_LABELS)
    pieces = {
        part.strip().lower()
        for part in raw_value.replace(";", ",").split(",")
        if part.strip()
    }
    if "all" in pieces:
        return set(CATEGORY_LABELS)
    return {piece for piece in pieces if piece in CATEGORY_LABELS}


def normalize_part_size_kb(value: int) -> int:
    size = max(4, min(512, int(value)))
    size -= size % 4
    return max(4, size)


def _read_bool(values: dict[str, str], key: str, default: bool) -> bool:
    raw = values.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(config_path: Path = DEFAULT_CONFIG_PATH) -> AppSettings:
    values = read_config(config_path)

    api_id = int(values.get("api_id", "0"))
    api_hash = values.get("api_hash", "").strip()
    source = normalize_source(values.get("source", ""))
    filter_preset = values.get("filter_preset", "Everything").strip() or "Everything"
    if filter_preset not in FILTER_PRESETS:
        filter_preset = "Custom"

    settings = AppSettings(
        api_id=api_id,
        api_hash=api_hash,
        source=source,
        destination=Path(values.get("destination", str(DEFAULT_DESTINATION))).expanduser().resolve(),
        session_path=Path(values.get("session", str(DEFAULT_SESSION_PATH))).expanduser().resolve(),
        state_db_path=Path(values.get("state_db", str(DEFAULT_STATE_DB_PATH))).expanduser().resolve(),
        categories=normalize_categories(values.get("include", "all")),
        extensions=normalize_extensions(values.get("extensions", "")),
        filter_preset=filter_preset,
        extract_archives=_read_bool(values, "extract_zips", False),
        keep_archives=_read_bool(values, "keep_archives", True),
        retries=max(1, int(values.get("retries", str(DEFAULT_RETRIES)))),
        workers=max(1, int(values.get("workers", str(DEFAULT_WORKERS)))),
        part_size_kb=normalize_part_size_kb(int(values.get("part_size_kb", str(DEFAULT_PART_SIZE_KB)))),
        ui_refresh_seconds=max(0.2, float(values.get("progress_interval", str(DEFAULT_UI_REFRESH_SECONDS)))),
        stall_timeout_seconds=max(20, int(values.get("stall_timeout", str(DEFAULT_STALL_TIMEOUT_SECONDS)))),
        benchmark_source=normalize_source(values.get("test_source", "")),
        benchmark_seconds=max(5, int(values.get("test_duration_seconds", str(DEFAULT_BENCHMARK_SECONDS)))),
        benchmark_sample_files=max(1, int(values.get("test_sample_size", str(DEFAULT_BENCHMARK_SAMPLE_FILES)))),
        benchmark_block_min_kb=normalize_part_size_kb(int(values.get("test_block_min", str(DEFAULT_BENCHMARK_BLOCK_MIN)))),
        benchmark_block_max_kb=normalize_part_size_kb(int(values.get("test_block_max", str(DEFAULT_BENCHMARK_BLOCK_MAX)))),
        benchmark_worker_max=max(1, int(values.get("test_worker_max", str(DEFAULT_BENCHMARK_WORKER_MAX)))),
    )

    if not settings.api_id or not settings.api_hash or not settings.source:
        raise ValueError("api_id, api_hash, and source must be configured.")
    return settings


def save_settings(settings: AppSettings, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    parser = configparser.ConfigParser()
    parser["telegram"] = {
        "api_id": str(settings.api_id),
        "api_hash": settings.api_hash,
        "source": settings.source,
    }
    parser["download"] = {
        "destination": str(settings.destination),
        "session": str(settings.session_path),
        "state_db": str(settings.state_db_path),
        "include": ",".join(sorted(settings.categories)) if settings.categories != set(CATEGORY_LABELS) else "all",
        "extensions": " ".join(sorted(settings.extensions)),
        "filter_preset": settings.filter_preset,
        "extract_zips": str(settings.extract_archives).lower(),
        "keep_archives": str(settings.keep_archives).lower(),
        "retries": str(settings.retries),
        "workers": str(settings.workers),
        "part_size_kb": str(settings.part_size_kb),
        "progress_interval": str(settings.ui_refresh_seconds),
        "stall_timeout": str(settings.stall_timeout_seconds),
        "test_source": settings.benchmark_source,
        "test_duration_seconds": str(settings.benchmark_seconds),
        "test_sample_size": str(settings.benchmark_sample_files),
        "test_block_min": str(settings.benchmark_block_min_kb),
        "test_block_max": str(settings.benchmark_block_max_kb),
        "test_worker_max": str(settings.benchmark_worker_max),
    }
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def build_settings(**overrides) -> AppSettings:
    """Build settings from direct values, mainly for the GUI and tests."""
    categories = overrides.pop("categories", set(CATEGORY_LABELS))
    extensions = overrides.pop("extensions", set())
    if isinstance(categories, str):
        categories = normalize_categories(categories)
    if isinstance(extensions, str):
        extensions = normalize_extensions(extensions)

    return AppSettings(
        api_id=int(overrides["api_id"]),
        api_hash=str(overrides["api_hash"]).strip(),
        source=normalize_source(str(overrides["source"])),
        destination=Path(overrides.get("destination", DEFAULT_DESTINATION)).expanduser().resolve(),
        session_path=Path(overrides.get("session_path", DEFAULT_SESSION_PATH)).expanduser().resolve(),
        state_db_path=Path(overrides.get("state_db_path", DEFAULT_STATE_DB_PATH)).expanduser().resolve(),
        categories=set(categories),
        extensions={normalize_extension(value) for value in extensions if normalize_extension(value)},
        filter_preset=str(overrides.get("filter_preset", "Custom")),
        extract_archives=bool(overrides.get("extract_archives", False)),
        keep_archives=bool(overrides.get("keep_archives", True)),
        retries=max(1, int(overrides.get("retries", DEFAULT_RETRIES))),
        workers=max(1, int(overrides.get("workers", DEFAULT_WORKERS))),
        part_size_kb=normalize_part_size_kb(int(overrides.get("part_size_kb", DEFAULT_PART_SIZE_KB))),
        ui_refresh_seconds=max(0.2, float(overrides.get("ui_refresh_seconds", DEFAULT_UI_REFRESH_SECONDS))),
        stall_timeout_seconds=max(20, int(overrides.get("stall_timeout_seconds", DEFAULT_STALL_TIMEOUT_SECONDS))),
        benchmark_source=normalize_source(str(overrides.get("benchmark_source", ""))),
        benchmark_seconds=max(5, int(overrides.get("benchmark_seconds", DEFAULT_BENCHMARK_SECONDS))),
        benchmark_sample_files=max(1, int(overrides.get("benchmark_sample_files", DEFAULT_BENCHMARK_SAMPLE_FILES))),
        benchmark_block_min_kb=normalize_part_size_kb(int(overrides.get("benchmark_block_min_kb", DEFAULT_BENCHMARK_BLOCK_MIN))),
        benchmark_block_max_kb=normalize_part_size_kb(int(overrides.get("benchmark_block_max_kb", DEFAULT_BENCHMARK_BLOCK_MAX))),
        benchmark_worker_max=max(1, int(overrides.get("benchmark_worker_max", DEFAULT_BENCHMARK_WORKER_MAX))),
    )


def read_config(config_path: Path) -> dict[str, str]:
    if not config_path.exists():
        return {}
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    values: dict[str, str] = {}
    for section in parser.sections():
        for key, value in parser.items(section):
            values[key] = value
    return values
