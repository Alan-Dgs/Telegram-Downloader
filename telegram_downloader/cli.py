from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, load_settings
from .engine import human_size, run_download


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download Telegram channel media with resume support.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="INI config file.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        settings = load_settings(args.config)
        def on_event(event_type: str, payload: dict) -> None:
            if event_type == "connected":
                print(
                    f"Connected to {payload.get('entity_name', settings.source)} "
                    f"| crypto={payload.get('crypto_backend')} "
                    f"| workers={payload.get('workers')} "
                    f"| block={payload.get('part_size_kb')}KB"
                )
            elif event_type == "scan_complete":
                summary = payload.get("summary", {})
                print(
                    f"Scan complete: {summary.get('pending', 0)} pending | "
                    f"{summary.get('skipped', 0)} already present | "
                    f"{human_size(summary.get('pending_bytes', 0))} queued"
                )
            elif event_type == "progress":
                active = payload.get("active_items", [])
                current = active[0]["label"] if active else "idle"
                print(
                    "\r"
                    f"{payload.get('completed_files', 0)}/{payload.get('total_jobs', 0)} | "
                    f"{payload.get('global_percent', 0.0):5.1f}% | "
                    f"remaining {payload.get('remaining_files', 0)} | "
                    f"{human_size(payload.get('speed_bps', 0))}/s | {current:<60}",
                    end="",
                    flush=True,
                )
            elif event_type == "log":
                print()
                print(f"[{payload.get('level', 'info').upper()}] {payload.get('message', '')}")
            elif event_type == "job_failed":
                print()
                print(f"[ERROR] {payload.get('name', 'file')} -> {payload.get('error', 'Unknown error')}")

        summary = asyncio.run(
            run_download(
                settings,
                allow_interactive_login=True,
                event_callback=on_event,
            )
        )
        print()
        print()
        print("Summary")
        print(f"  Files detected : {summary['seen']}")
        print(f"  Downloaded     : {summary['downloaded']}")
        print(f"  Already done   : {summary['skipped']}")
        print(f"  Failed         : {summary['failed']}")
        return 0
    except KeyboardInterrupt:
        print("\nStopped by user.")
        return 130
    except Exception as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
