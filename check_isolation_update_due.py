"""Decide whether a scheduled isolation update is due, using committed output."""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def update_due(summary_path, event_name, now=None, min_age_minutes=50):
    now = now or datetime.now(timezone.utc)
    if event_name != "schedule":
        return True, "An explicitly triggered run always refreshes the output."
    try:
        summary = json.loads(Path(summary_path).read_text(encoding="utf-8"))
        timestamp = summary["created_at"]
        generated = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return True, "Previous output has no usable timestamp; refresh required."
    age = now - generated
    if age < timedelta(minutes=-5):
        return True, "Previous output is dated in the future; refresh required."
    due = age >= timedelta(minutes=min_age_minutes)
    decision = "refresh required" if due else "output is recent; skip this check"
    return due, f"Previous output: {generated.isoformat()} ({age.total_seconds() / 60:.1f} minutes old); {decision}."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", default="out_isolation_current/qld_isolation_summary.json")
    parser.add_argument("--min-age-minutes", type=int, default=50)
    args = parser.parse_args()
    if args.min_age_minutes <= 0:
        parser.error("--min-age-minutes must be positive")
    event = os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch")
    due, reason = update_due(args.summary, event, min_age_minutes=args.min_age_minutes)
    print(f"Trigger: {event}. {reason}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"run_required={'true' if due else 'false'}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(f"## Isolation update check\n\nTrigger: `{event}`. {reason}\n")


if __name__ == "__main__":
    main()
