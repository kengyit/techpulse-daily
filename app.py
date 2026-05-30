#!/usr/bin/env python3
"""TechPulse Daily — local application runner & scheduler.

Replaces the OpenClaw cron job with a self-contained local application. Runs the
briefing pipeline (and optionally the Idea Radar) either once or on a recurring
daily schedule via a built-in, dependency-free scheduler.

Usage:
    python app.py once                 # run the pipeline a single time, then exit
    python app.py serve                # run forever, firing daily at the schedule time
    python app.py serve --at 06:30     # override the daily time for this run
    python app.py --help

Behaviour is read from categories.json `settings` and overridden by env vars:
    TECHPULSE_SCHEDULE         "HH:MM" 24h local time   (default 06:30)
    TECHPULSE_RUN_IDEA_RADAR   "1" to also run idea_radar after each briefing
    TECHPULSE_TELEGRAM         "1" to deliver via Telegram
    TECHPULSE_JSON             "1" to also write structured JSON   (default on)
    TECHPULSE_NO_LLM           "1" to skip the LLM and use raw RSS summaries

For OS-managed scheduling (systemd / launchd) instead of `serve`, see install.sh.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DEFAULT_CONFIG = SCRIPT_DIR / "categories.json"
ENV_FILE = SCRIPT_DIR / ".env"
LOG_FILE = SCRIPT_DIR / "techpulse.log"

_stop = False


def load_env_file(path: Path = ENV_FILE) -> None:
    """Load KEY=VALUE lines from .env into the environment (without overriding).

    launchd does not source .env the way the systemd units / cron.sh do, so the
    daemon reads it here. Existing env vars win, so shell/launchd overrides hold.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.split(" #", 1)[0].strip().strip('"').strip("'")  # drop inline comments + quotes
        if key:
            os.environ.setdefault(key, val)


def _handle_signal(signum, _frame):
    global _stop
    _stop = True
    log(f"Received signal {signum}; will stop after the current cycle.")


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} — {msg}"
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_settings(config_path: Path) -> dict:
    try:
        return json.loads(Path(config_path).read_text(encoding="utf-8")).get("settings", {})
    except (OSError, json.JSONDecodeError):
        return {}


def _flag(env_name: str, settings: dict, settings_key: str, default: bool) -> bool:
    raw = os.environ.get(env_name)
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(settings.get(settings_key, default))


def parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.strip().split(":")
    hour, minute = int(hour), int(minute)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"schedule time out of range: {value!r}")
    return hour, minute


def next_run(now: datetime, hour: int, minute: int) -> datetime:
    """Next occurrence of HH:MM at or after `now` (today, else tomorrow)."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def run_once(opts: argparse.Namespace) -> None:
    """Run the briefing pipeline once, then the optional Idea Radar."""
    # Lazy import: feedparser (and its network use) is only needed here, so the
    # scheduler can start and report config even before a single run fires.
    import run

    log("Briefing pipeline starting.")
    try:
        _, stats = run.run_pipeline(
            config_path=opts.config,
            use_llm=not opts.no_llm,
            telegram=opts.telegram,
            json_out=opts.json,
        )
        log(
            f"Briefing complete: {stats.get('selected', 0)} stories, "
            f"{stats.get('feeds_ok', 0)}/{stats.get('feeds_total', 0)} feeds, "
            f"{stats.get('elapsed_sec', 0):.1f}s."
        )
    except Exception as exc:  # noqa: BLE001 — log and keep the daemon alive
        log(f"Briefing pipeline FAILED: {exc}")
        return

    if opts.idea_radar:
        import idea_radar
        try:
            idea_radar.run_radar(use_llm=not opts.no_llm, from_json=opts.json)
            log("Idea Radar complete.")
        except RuntimeError as exc:
            log(f"Idea Radar skipped: {exc}")
        except Exception as exc:  # noqa: BLE001 — never let the optional stage kill the daemon
            log(f"Idea Radar FAILED: {exc}")


def serve(opts: argparse.Namespace) -> None:
    """Run forever, firing the pipeline once per day at the schedule time."""
    hour, minute = parse_hhmm(opts.at)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log(f"Scheduler started; daily run at {opts.at} local time. (Ctrl-C / SIGTERM to stop.)")
    while not _stop:
        target = next_run(datetime.now(), hour, minute)
        log(f"Next run scheduled for {target:%Y-%m-%d %H:%M}.")
        while not _stop and datetime.now() < target:
            remaining = (target - datetime.now()).total_seconds()
            time.sleep(max(0.0, min(30.0, remaining)))
        if _stop:
            break
        run_once(opts)
    log("Scheduler stopped.")


def main() -> None:
    load_env_file()
    settings = {}
    # Peek at --config early so settings-derived defaults are correct.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=str(DEFAULT_CONFIG))
    known, _ = pre.parse_known_args()
    settings = load_settings(Path(known.config))

    default_schedule = os.environ.get("TECHPULSE_SCHEDULE") or str(settings.get("schedule_time", "06:30"))

    parser = argparse.ArgumentParser(
        description="TechPulse Daily — local runner & scheduler (replaces the OpenClaw cron job).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("mode", choices=["once", "serve"], nargs="?", default="once",
                        help="once: run a single time (default). serve: run on a daily schedule.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to categories.json")
    parser.add_argument("--at", default=default_schedule, metavar="HH:MM",
                        help=f"Daily run time for serve mode (default {default_schedule}).")
    parser.add_argument("--no-llm", action="store_true", default=_flag("TECHPULSE_NO_LLM", settings, "no_llm", False),
                        help="Skip the LLM; use raw RSS summaries.")
    parser.add_argument("--telegram", action="store_true", default=_flag("TECHPULSE_TELEGRAM", settings, "telegram", False),
                        help="Deliver the briefing via Telegram.")
    parser.add_argument("--json", action="store_true", default=_flag("TECHPULSE_JSON", settings, "json", True),
                        help="Also write structured JSON output.")
    parser.add_argument("--idea-radar", dest="idea_radar", action="store_true",
                        default=_flag("TECHPULSE_RUN_IDEA_RADAR", settings, "run_idea_radar", False),
                        help="Also run the Idea Radar after each briefing.")
    opts = parser.parse_args()

    if opts.mode == "serve":
        serve(opts)
    else:
        run_once(opts)


if __name__ == "__main__":
    main()
