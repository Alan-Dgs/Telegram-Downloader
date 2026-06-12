from __future__ import annotations

import asyncio
import contextlib
import getpass
import os
import re
import time
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from telethon import TelegramClient, errors, functions, utils
from telethon.crypto import aes

from .config import AppSettings
from .state import StateStore

ARCHIVE_EXTENSIONS = {
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".zst",
    ".iso",
}
ARCHIVE_MIME_TYPES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/x-7z-compressed",
    "application/vnd.rar",
    "application/x-rar-compressed",
    "application/gzip",
    "application/x-tar",
}
AUDIO_EXTENSIONS = {
    ".mp3",
    ".flac",
    ".wav",
    ".aac",
    ".m4a",
    ".ogg",
    ".opus",
    ".wma",
    ".aiff",
    ".alac",
}
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".webm",
    ".m4v",
}
IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
}

EventCallback = Callable[[str, dict[str, Any]], None]


@dataclass(slots=True)
class PreparedDownload:
    message_id: int
    category: str
    file_name: str
    target_path: Path
    file_size: int | None


@dataclass(slots=True)
class ActiveTransfer:
    message_id: int
    label: str
    current: int
    total: int
    started_at: float
    last_update: float


@dataclass(slots=True)
class ScanResult:
    entity_name: str
    summary: dict[str, int]
    jobs: list[PreparedDownload]
    scanned_messages: int


class DownloadController:
    """Mutable controller shared between the GUI and the engine."""

    def __init__(self) -> None:
        self.stop_after_active = False

    def request_graceful_stop(self) -> None:
        self.stop_after_active = True


def emit(callback: EventCallback | None, event_type: str, **payload: Any) -> None:
    if callback is not None:
        callback(event_type, payload)


def detect_crypto_backend() -> str:
    if getattr(aes, "cryptg", None):
        return "cryptg"
    if getattr(aes.libssl, "encrypt_ige", None) and getattr(aes.libssl, "decrypt_ige", None):
        return "libssl"
    return "pyaes"


def human_size(size: int | float | None) -> str:
    if size is None:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\\\|?*]+', "_", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:180] or "file"


def parse_entity_ref(source: str) -> str | int:
    stripped = source.strip()
    if re.fullmatch(r"-?\d+", stripped):
        return int(stripped)
    return stripped


async def connect_client(
    settings: AppSettings,
    *,
    allow_interactive_login: bool,
) -> TelegramClient:
    client = TelegramClient(
        str(settings.session_path),
        settings.api_id,
        settings.api_hash,
        flood_sleep_threshold=120,
    )
    if allow_interactive_login:
        await client.start(
            phone=lambda: input("Telegram phone number (international format, ex: +336...): ").strip(),
            password=lambda: getpass.getpass("Telegram 2FA password: "),
        )
    else:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telegram session missing or expired. Run the console launcher once to sign in."
            )
    return client


async def resolve_entity(client: TelegramClient, source: str):
    reference = parse_entity_ref(source)
    try:
        return await client.get_entity(reference)
    except ValueError:
        pass

    normalized_source = source.strip().lower()
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        peer_id = str(utils.get_peer_id(entity))
        title = (getattr(entity, "title", None) or dialog.name or "").strip().lower()
        username = (getattr(entity, "username", None) or "").strip().lower()

        if peer_id == normalized_source:
            return entity
        if username and normalized_source in {username, f"@{username}"}:
            return entity
        if title and title == normalized_source:
            return entity

    raise ValueError(
        f'Unable to resolve channel "{source}". Use the -100... ID or a channel name already visible in your account.'
    )


async def fetch_channel_info(
    settings: AppSettings,
    *,
    allow_interactive_login: bool = False,
) -> dict[str, Any]:
    client = await connect_client(settings, allow_interactive_login=allow_interactive_login)
    try:
        entity = await resolve_entity(client, settings.source)
        info: dict[str, Any] = {
            "title": getattr(entity, "title", None) or getattr(entity, "first_name", None) or settings.source,
            "username": getattr(entity, "username", None),
            "id": utils.get_peer_id(entity),
            "type": entity.__class__.__name__,
        }
        with contextlib.suppress(Exception):
            if hasattr(entity, "megagroup") or hasattr(entity, "broadcast"):
                full = await client(functions.channels.GetFullChannelRequest(entity))
                info["about"] = getattr(full.full_chat, "about", None)
                info["participants_count"] = getattr(full.full_chat, "participants_count", None)
        return info
    finally:
        await client.disconnect()


def display_name(entity) -> str:
    title = getattr(entity, "title", None)
    username = getattr(entity, "username", None)
    if title and username:
        return f"{title} (@{username})"
    return title or username or str(getattr(entity, "id", "unknown"))


def classify_message(message) -> str:
    file_name = (getattr(message.file, "name", None) or "").lower()
    ext = Path(file_name).suffix.lower()
    mime_type = (getattr(message.file, "mime_type", None) or "").lower()

    if ext in ARCHIVE_EXTENSIONS or mime_type in ARCHIVE_MIME_TYPES:
        return "archives"
    if getattr(message, "voice", None):
        return "voice"
    if getattr(message, "audio", None) or mime_type.startswith("audio/") or ext in AUDIO_EXTENSIONS:
        return "audio"
    if getattr(message, "video", None) or mime_type.startswith("video/") or ext in VIDEO_EXTENSIONS:
        return "video"
    if getattr(message, "photo", None) or mime_type.startswith("image/") or ext in IMAGE_EXTENSIONS:
        return "images"
    return "documents"


def guess_extension(message, category: str) -> str:
    mime_type = (getattr(message.file, "mime_type", None) or "").lower()
    if "zip" in mime_type:
        return ".zip"
    defaults = {
        "audio": ".mp3",
        "video": ".mp4",
        "images": ".jpg",
        "voice": ".ogg",
        "archives": ".zip",
    }
    return defaults.get(category, "")


def choose_file_name(message, category: str) -> str:
    original_name = getattr(message.file, "name", None)
    if original_name:
        original_name = safe_name(Path(original_name).name)
        stem = safe_name(Path(original_name).stem)
        suffix = Path(original_name).suffix
        if stem:
            return f"{stem}__msg{message.id}{suffix}"

    ext = (getattr(message.file, "ext", None) or "").strip()
    ext = ext if ext.startswith(".") else f".{ext}" if ext else ""
    if not ext:
        ext = guess_extension(message, category)
    return f"{category}_{message.id}{ext}"


def build_target_path(destination: Path, message, category: str) -> Path:
    return destination / choose_file_name(message, category)


def file_matches(path: Path, expected_size: int | None) -> bool:
    if not path.exists():
        return False
    if expected_size is None:
        return True
    try:
        return path.stat().st_size == expected_size
    except OSError:
        return False


def is_already_complete(
    store: StateStore,
    message_id: int,
    target_path: Path,
    expected_size: int | None,
) -> bool:
    row = store.get(message_id)
    if row and row["file_path"] and file_matches(Path(row["file_path"]), expected_size):
        return True
    return file_matches(target_path, expected_size)


def should_include(category: str, settings: AppSettings, target_path: Path) -> bool:
    if settings.categories and category not in settings.categories:
        return False
    if settings.extensions:
        return target_path.suffix.lower() in settings.extensions
    return True


def extract_zip(archive_path: Path, destination: Path) -> Path:
    marker_path = destination / f".{archive_path.stem}.extracted.ok"
    if marker_path.exists():
        return marker_path

    extracted_any = False
    with zipfile.ZipFile(archive_path) as archive:
        root_resolved = destination.resolve()
        for member in archive.infolist():
            cleaned_name = member.filename.replace("\\", "/").rstrip("/")
            if not cleaned_name:
                continue
            member_path = (destination / cleaned_name).resolve()
            if not str(member_path).startswith(str(root_resolved)):
                raise RuntimeError(f"Unsafe ZIP member blocked: {member.filename}")
        for member in archive.infolist():
            if member.is_dir():
                continue
            final_name = safe_name(Path(member.filename).name)
            if not final_name:
                continue
            final_path = available_path(destination / final_name, archive_path.stem)
            with archive.open(member) as source, open(final_path, "wb") as target:
                target.write(source.read())
            extracted_any = True
    if extracted_any:
        marker_path.write_text(f"source={archive_path}\n", encoding="utf-8")
    return marker_path


def available_path(path: Path, suffix_seed: str) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    candidate = parent / f"{stem}__{suffix_seed}{suffix}"
    while candidate.exists():
        candidate = parent / f"{stem}__{suffix_seed}_{counter}{suffix}"
        counter += 1
    return candidate


def prepared_to_dict(job: PreparedDownload) -> dict[str, Any]:
    return {
        "message_id": job.message_id,
        "name": job.file_name,
        "path": str(job.target_path),
        "category": job.category,
        "file_size": job.file_size or 0,
    }


class ProgressTracker:
    """Tracks active downloads and emits compact snapshots for the UI."""

    def __init__(self, event_callback: EventCallback | None) -> None:
        self.event_callback = event_callback
        self.started_at = time.monotonic()
        self.completed_files = 0
        self.failed_files = 0
        self.completed_bytes = 0
        self.total_jobs = 0
        self.total_bytes = 0
        self.initial_completed_files = 0
        self.initial_completed_bytes = 0
        self.active: dict[int, ActiveTransfer] = {}

    def start(
        self,
        total_jobs: int,
        total_bytes: int,
        *,
        initial_completed_files: int = 0,
        initial_completed_bytes: int = 0,
    ) -> None:
        self.total_jobs = total_jobs
        self.total_bytes = total_bytes
        self.started_at = time.monotonic()
        self.completed_files = 0
        self.failed_files = 0
        self.completed_bytes = 0
        self.initial_completed_files = initial_completed_files
        self.initial_completed_bytes = initial_completed_bytes
        self.active.clear()
        self.emit()

    def start_transfer(self, message_id: int, label: str, total: int | None) -> Callable[[int, int], None]:
        self.active[message_id] = ActiveTransfer(
            message_id=message_id,
            label=label[:64],
            current=0,
            total=total or 0,
            started_at=time.monotonic(),
            last_update=time.monotonic(),
        )
        self.emit()

        def callback(current: int, total: int) -> None:
            transfer = self.active.get(message_id)
            if transfer is None:
                return
            transfer.current = current
            transfer.total = total or transfer.total
            transfer.last_update = time.monotonic()
            self.emit()

        return callback

    def mark_done(self, message_id: int, file_size: int | None) -> None:
        transfer = self.active.pop(message_id, None)
        self.completed_files += 1
        self.completed_bytes += file_size or (transfer.current if transfer else 0)
        self.emit()

    def clear_transfer(self, message_id: int) -> None:
        self.active.pop(message_id, None)
        self.emit()

    def mark_failed(self, message_id: int) -> None:
        self.active.pop(message_id, None)
        self.failed_files += 1
        self.emit()

    def emit(self) -> None:
        active_bytes = sum(item.current for item in self.active.values())
        done_bytes = self.initial_completed_bytes + self.completed_bytes + active_bytes
        completed_files = self.initial_completed_files + self.completed_files
        elapsed = max(time.monotonic() - self.started_at, 0.001)
        payload = {
            "completed_files": completed_files,
            "processed_files": completed_files + self.failed_files,
            "failed_files": self.failed_files,
            "historical_files": self.initial_completed_files,
            "total_jobs": self.total_jobs,
            "completed_bytes": done_bytes,
            "total_bytes": self.total_bytes,
            "global_percent": (done_bytes / self.total_bytes * 100) if self.total_bytes else 0.0,
            "remaining_files": max(self.total_jobs - (completed_files + self.failed_files), 0),
            "remaining_bytes": max(self.total_bytes - done_bytes, 0),
            "speed_bps": max(done_bytes - self.initial_completed_bytes, 0) / elapsed,
            "active_items": [
                {
                    "message_id": item.message_id,
                    "label": item.label,
                    "current": item.current,
                    "total": item.total,
                    "percent": (item.current / item.total * 100) if item.total else 0.0,
                    "speed_bps": item.current / max(time.monotonic() - item.started_at, 0.001),
                }
                for item in sorted(self.active.values(), key=lambda x: x.started_at)
            ],
        }
        emit(self.event_callback, "progress", **payload)


async def scan_channel(
    settings: AppSettings,
    *,
    event_callback: EventCallback | None = None,
    allow_interactive_login: bool = False,
) -> ScanResult:
    settings.destination.mkdir(parents=True, exist_ok=True)
    settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    settings.state_db_path.parent.mkdir(parents=True, exist_ok=True)

    store = StateStore(settings.state_db_path)
    client = await connect_client(settings, allow_interactive_login=allow_interactive_login)

    try:
        entity = await resolve_entity(client, settings.source)
        entity_name = display_name(entity)
        emit(
            event_callback,
            "connected",
            entity_name=entity_name,
            entity_title=getattr(entity, "title", None),
            username=getattr(entity, "username", None),
            channel_id=utils.get_peer_id(entity),
            entity_type=entity.__class__.__name__,
            source=settings.source,
            crypto_backend=detect_crypto_backend(),
            workers=settings.workers,
            part_size_kb=settings.part_size_kb,
            recent_done=store.list_completed(250),
            recent_failed=store.list_failed(250),
            history_counts=store.counts(),
        )
        with contextlib.suppress(Exception):
            if hasattr(entity, "megagroup") or hasattr(entity, "broadcast"):
                full = await client(functions.channels.GetFullChannelRequest(entity))
                emit(
                    event_callback,
                    "channel_details",
                    about=getattr(full.full_chat, "about", None),
                    participants_count=getattr(full.full_chat, "participants_count", None),
                )

        summary = {
            "seen": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "pending": 0,
            "pending_bytes": 0,
            "skipped_bytes": 0,
        }
        jobs: list[PreparedDownload] = []
        scanned_messages = 0

        emit(event_callback, "log", level="info", message="Scan started.")
        async for message in client.iter_messages(entity, reverse=True):
            scanned_messages += 1
            if scanned_messages % 250 == 0:
                emit(
                    event_callback,
                    "scan_progress",
                    scanned_messages=scanned_messages,
                    matched_files=summary["seen"],
                )

            if not getattr(message, "file", None):
                continue

            category = classify_message(message)
            target_path = build_target_path(settings.destination, message, category)
            if not should_include(category, settings, target_path):
                continue

            summary["seen"] += 1
            file_size = getattr(message.file, "size", None)
            if is_already_complete(store, message.id, target_path, file_size):
                store.mark_skipped(message.id, category, target_path, file_size)
                summary["skipped"] += 1
                summary["skipped_bytes"] += file_size or 0
                continue

            jobs.append(
                PreparedDownload(
                    message_id=message.id,
                    category=category,
                    file_name=target_path.name,
                    target_path=target_path,
                    file_size=file_size,
                )
            )
            summary["pending"] += 1
            summary["pending_bytes"] += file_size or 0

        jobs.sort(key=lambda item: (item.file_size or 0, item.message_id), reverse=True)
        emit(
            event_callback,
            "scan_complete",
            entity_name=entity_name,
            summary=summary,
            pending_jobs=[prepared_to_dict(job) for job in jobs],
            scanned_messages=scanned_messages,
        )
        emit(event_callback, "log", level="info", message="Scan complete.")
        return ScanResult(
            entity_name=entity_name,
            summary=summary,
            jobs=jobs,
            scanned_messages=scanned_messages,
        )
    finally:
        await client.disconnect()
        store.close()


async def _download_media(
    client: TelegramClient,
    message,
    destination: Path,
    settings: AppSettings,
    file_size: int | None,
    progress_callback: Callable[[int, int], None],
) -> None:
    try:
        await client.download_file(
            message,
            file=str(destination),
            part_size_kb=settings.part_size_kb,
            file_size=file_size,
            progress_callback=progress_callback,
        )
    except (AttributeError, TypeError, ValueError):
        await client.download_media(message, file=str(destination), progress_callback=progress_callback)


async def _download_one(
    client: TelegramClient,
    entity,
    settings: AppSettings,
    store: StateStore,
    progress: ProgressTracker,
    prepared: PreparedDownload,
    event_callback: EventCallback | None,
) -> bool:
    temp_path = prepared.target_path.with_suffix(prepared.target_path.suffix + ".part")
    temp_path.unlink(missing_ok=True)

    for attempt in range(1, settings.retries + 1):
        message = await client.get_messages(entity, ids=prepared.message_id)
        if message is None or not getattr(message, "file", None):
            store.mark_failed(prepared.message_id, prepared.category, prepared.target_path, "Message no longer available.")
            progress.mark_failed(prepared.message_id)
            emit(
                event_callback,
                "job_failed",
                message_id=prepared.message_id,
                name=prepared.file_name,
                path=str(prepared.target_path),
                category=prepared.category,
                error="Message no longer available.",
            )
            return False

        last_progress = {"at": time.monotonic(), "bytes": 0}
        stalled = False

        def on_progress(current: int, total: int) -> None:
            last_progress["at"] = time.monotonic()
            last_progress["bytes"] = current
            progress_callback(current, total)

        progress_callback = progress.start_transfer(prepared.message_id, prepared.file_name, prepared.file_size)
        try:
            emit(event_callback, "job_started", job=prepared_to_dict(prepared), attempt=attempt)
            download_task = asyncio.create_task(
                _download_media(
                    client=client,
                    message=message,
                    destination=temp_path,
                    settings=settings,
                    file_size=prepared.file_size,
                    progress_callback=on_progress,
                )
            )

            async def stall_guard() -> None:
                nonlocal stalled
                while not download_task.done():
                    await asyncio.sleep(5)
                    if time.monotonic() - last_progress["at"] >= settings.stall_timeout_seconds:
                        stalled = True
                        download_task.cancel()
                        return

            guard_task = asyncio.create_task(stall_guard())
            try:
                await download_task
            except asyncio.CancelledError:
                if stalled:
                    raise RuntimeError(
                        f"Download stalled for {settings.stall_timeout_seconds}s at {human_size(last_progress['bytes'])}."
                    )
                raise
            finally:
                guard_task.cancel()
                await asyncio.gather(guard_task, return_exceptions=True)

            if not temp_path.exists():
                raise FileNotFoundError("Temporary file was not created.")

            final_path = prepared.target_path
            if final_path.exists() and not file_matches(final_path, prepared.file_size):
                final_path = available_path(final_path, f"msg{prepared.message_id}")
            os.replace(temp_path, final_path)

            extracted_path: Path | None = None
            if settings.extract_archives and final_path.suffix.lower() == ".zip":
                extracted_path = extract_zip(final_path, settings.destination)
                if extracted_path and not settings.keep_archives:
                    final_path.unlink(missing_ok=True)

            store.mark_done(
                prepared.message_id,
                prepared.category,
                final_path,
                prepared.file_size,
                extracted_path=extracted_path,
            )
            progress.mark_done(prepared.message_id, prepared.file_size)
            emit(
                event_callback,
                "job_done",
                message_id=prepared.message_id,
                name=final_path.name,
                path=str(final_path),
                category=prepared.category,
                file_size=prepared.file_size or 0,
            )
            return True
        except errors.FloodWaitError as exc:
            progress.clear_transfer(prepared.message_id)
            wait_seconds = int(getattr(exc, "seconds", 0) or getattr(exc, "value", 0) or 10)
            emit(event_callback, "log", level="warning", message=f"Flood wait: sleeping {wait_seconds}s.")
            await asyncio.sleep(wait_seconds + 1)
        except (errors.RPCError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
            temp_path.unlink(missing_ok=True)
            if attempt >= settings.retries:
                progress.mark_failed(prepared.message_id)
                store.mark_failed(prepared.message_id, prepared.category, prepared.target_path, str(exc))
                emit(
                    event_callback,
                    "job_failed",
                    message_id=prepared.message_id,
                    name=prepared.file_name,
                    path=str(prepared.target_path),
                    category=prepared.category,
                    error=str(exc),
                )
                return False
            progress.clear_transfer(prepared.message_id)
            backoff = min(30, 5 * attempt)
            emit(
                event_callback,
                "log",
                level="warning",
                message=f"Retry {attempt}/{settings.retries} for {prepared.file_name}: {exc}",
            )
            await asyncio.sleep(backoff)
        finally:
            temp_path.unlink(missing_ok=True)

    return False


async def run_download(
    settings: AppSettings,
    *,
    scan_result: ScanResult | None = None,
    event_callback: EventCallback | None = None,
    controller: DownloadController | None = None,
    allow_interactive_login: bool = False,
) -> dict[str, int]:
    controller = controller or DownloadController()
    if scan_result is None:
        scan_result = await scan_channel(
            settings,
            event_callback=event_callback,
            allow_interactive_login=allow_interactive_login,
        )

    client = await connect_client(settings, allow_interactive_login=allow_interactive_login)
    store = StateStore(settings.state_db_path)
    progress = ProgressTracker(event_callback)
    summary = dict(scan_result.summary)

    try:
        entity = await resolve_entity(client, settings.source)
        total_jobs = int(summary.get("seen", 0) or (summary.get("pending", 0) + summary.get("skipped", 0)))
        total_bytes = int(summary.get("pending_bytes", 0)) + int(summary.get("skipped_bytes", 0))
        initial_completed_files = int(summary.get("skipped", 0))
        initial_completed_bytes = int(summary.get("skipped_bytes", 0))
        jobs: list[PreparedDownload] = []
        for job in scan_result.jobs:
            if is_already_complete(store, job.message_id, job.target_path, job.file_size):
                store.mark_skipped(job.message_id, job.category, job.target_path, job.file_size)
                summary["skipped"] = summary.get("skipped", 0) + 1
                summary["skipped_bytes"] = summary.get("skipped_bytes", 0) + int(job.file_size or 0)
                initial_completed_files += 1
                initial_completed_bytes += int(job.file_size or 0)
                continue
            jobs.append(job)

        summary["pending"] = len(jobs)
        summary["pending_bytes"] = sum(job.file_size or 0 for job in jobs)
        progress.start(
            total_jobs=total_jobs,
            total_bytes=total_bytes,
            initial_completed_files=initial_completed_files,
            initial_completed_bytes=initial_completed_bytes,
        )
        emit(event_callback, "preparing_downloads", pending_jobs=len(jobs))
        emit(event_callback, "log", level="info", message="Download session started.")

        queue: asyncio.Queue[PreparedDownload | None] = asyncio.Queue()
        for job in jobs:
            queue.put_nowait(job)
        worker_count = min(settings.workers, max(len(jobs), 1))
        for _ in range(worker_count):
            queue.put_nowait(None)

        async def worker() -> None:
            while True:
                if controller.stop_after_active:
                    emit(event_callback, "log", level="info", message="Graceful stop requested. No new files will start.")
                    return
                prepared = await queue.get()
                try:
                    if prepared is None:
                        return
                    if controller.stop_after_active:
                        queue.put_nowait(prepared)
                        continue
                    success = await _download_one(
                        client=client,
                        entity=entity,
                        settings=settings,
                        store=store,
                        progress=progress,
                        prepared=prepared,
                        event_callback=event_callback,
                    )
                    if success:
                        summary["downloaded"] += 1
                    else:
                        summary["failed"] += 1
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        await asyncio.gather(*workers)
        emit(event_callback, "summary", **summary)
        emit(event_callback, "log", level="info", message="Download session finished.")
        return summary
    finally:
        await client.disconnect()
        store.close()


async def benchmark_settings(
    settings: AppSettings,
    *,
    scan_result: ScanResult | None = None,
    event_callback: EventCallback | None = None,
    allow_interactive_login: bool = False,
) -> dict[str, Any]:
    if scan_result is None:
        benchmark_settings_object = replace(
            settings,
            source=settings.benchmark_source or settings.source,
        )
        scan_result = await scan_channel(
            benchmark_settings_object,
            event_callback=event_callback,
            allow_interactive_login=allow_interactive_login,
        )

    source = settings.benchmark_source or settings.source
    profiles: list[tuple[int, int]] = []
    block_mid = max(4, min(512, (settings.benchmark_block_min_kb + settings.benchmark_block_max_kb) // 2))
    for candidate in [
        (1, settings.benchmark_block_min_kb),
        (1, settings.benchmark_block_max_kb),
        (min(2, settings.benchmark_worker_max), block_mid),
        (min(3, settings.benchmark_worker_max), settings.benchmark_block_max_kb),
        (settings.benchmark_worker_max, settings.benchmark_block_max_kb),
    ]:
        normalized = (max(1, candidate[0]), max(4, min(512, candidate[1] - candidate[1] % 4)))
        if normalized not in profiles:
            profiles.append(normalized)

    emit(
        event_callback,
        "benchmark_plan",
        combinations=[{"workers": workers, "part_size_kb": block} for workers, block in profiles],
        sample_size=settings.benchmark_sample_files,
        duration_seconds=settings.benchmark_seconds,
        benchmark_source=source,
    )

    benchmark_jobs = scan_result.jobs[: max(settings.benchmark_sample_files, settings.benchmark_worker_max)]
    if not benchmark_jobs:
        raise RuntimeError("No downloadable files were found for the benchmark.")
    emit(
        event_callback,
        "benchmark_jobs_selected",
        jobs=[prepared_to_dict(job) for job in benchmark_jobs],
    )

    client = await connect_client(settings, allow_interactive_login=allow_interactive_login)
    results: list[dict[str, Any]] = []

    try:
        entity = await resolve_entity(client, source)
        message_map: dict[int, Any] = {}
        for index in range(0, len(benchmark_jobs), 100):
            ids = [job.message_id for job in benchmark_jobs[index:index + 100]]
            for message in await client.get_messages(entity, ids=ids):
                if message is not None:
                    message_map[int(message.id)] = message

        for index, (workers, part_size_kb) in enumerate(profiles, start=1):
            emit(
                event_callback,
                "benchmark_case_started",
                index=index,
                total=len(profiles),
                workers=workers,
                part_size_kb=part_size_kb,
                duration_seconds=settings.benchmark_seconds,
            )
            started = time.monotonic()
            counters = {"bytes": 0}
            active_jobs = benchmark_jobs[: min(workers, len(benchmark_jobs))]
            deadline = started + settings.benchmark_seconds

            async def run_job(job: PreparedDownload) -> None:
                temp_path = settings.destination / "_benchmark" / f"{job.target_path.stem}__bench.part"
                temp_path.parent.mkdir(parents=True, exist_ok=True)
                message = message_map[job.message_id]

                def on_progress(current: int, total: int) -> None:
                    counters["bytes_map"][job.message_id] = current

                counters["bytes_map"] = counters.get("bytes_map", {})
                task = asyncio.create_task(
                    _download_media(
                        client=client,
                        message=message,
                        destination=temp_path,
                        settings=replace(settings, part_size_kb=part_size_kb),
                        file_size=job.file_size,
                        progress_callback=on_progress,
                    )
                )
                while not task.done():
                    if time.monotonic() >= deadline:
                        task.cancel()
                        break
                    await asyncio.sleep(0.25)
                await asyncio.gather(task, return_exceptions=True)
                temp_path.unlink(missing_ok=True)

            tasks = [asyncio.create_task(run_job(job)) for job in active_jobs]
            while True:
                elapsed = time.monotonic() - started
                transferred = sum(counters.get("bytes_map", {}).values())
                emit(
                    event_callback,
                    "benchmark_case_progress",
                    index=index,
                    total=len(profiles),
                    workers=workers,
                    part_size_kb=part_size_kb,
                    elapsed_seconds=elapsed,
                    duration_seconds=settings.benchmark_seconds,
                    transferred_bytes=transferred,
                    avg_speed_bps=transferred / max(elapsed, 0.001),
                    percent=min(100.0, elapsed / settings.benchmark_seconds * 100.0),
                    active_jobs=len(active_jobs),
                )
                if elapsed >= settings.benchmark_seconds or all(task.done() for task in tasks):
                    break
                await asyncio.sleep(0.5)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            elapsed = max(time.monotonic() - started, 0.001)
            transferred = sum(counters.get("bytes_map", {}).values())
            result = {
                "workers": workers,
                "part_size_kb": part_size_kb,
                "elapsed_seconds": elapsed,
                "transferred_bytes": transferred,
                "avg_speed_bps": transferred / elapsed,
            }
            results.append(result)
            emit(event_callback, "benchmark_case_done", **result)

        best = max(results, key=lambda item: item["avg_speed_bps"]) if results else None
        payload = {"results": results, "best_result": best}
        emit(event_callback, "benchmark_complete", **payload)
        return payload
    finally:
        await client.disconnect()
