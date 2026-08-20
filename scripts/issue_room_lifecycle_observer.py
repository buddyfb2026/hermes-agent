#!/usr/bin/env python3
"""Archive/reopen Hermes issue-room records from authoritative Linear state.

Reads credentials at runtime, polls only issue ids already present in issue-room
records, and atomically moves records between active/archive. It never changes a
Linear issue and never copies credentials into an artifact.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

HOME = Path.home()
ROOT = HOME / ".hermes" / "issue-rooms"
ACTIVE = ROOT / "active"
ARCHIVE = ROOT / "archive"
LINEAR_URL = "https://api.linear.app/graphql"
QUERY = "query IssueRoomState($id:String!){issue(id:$id){identifier state{name type}}}"


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def api_key() -> str:
    if os.environ.get("LINEAR_API_KEY"):
        return os.environ["LINEAR_API_KEY"]
    env = load_env(HOME / ".hermes" / ".env")
    return env.get("LINEAR_HERMES_API_KEY") or env.get("LINEAR_API_KEY") or ""


def issue_state(issue_id: str, key: str) -> str:
    body = json.dumps({"query": QUERY, "variables": {"id": issue_id}}).encode()
    req = urllib.request.Request(
        LINEAR_URL,
        data=body,
        headers={"Authorization": key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        payload = json.load(response)
    if payload.get("errors"):
        raise RuntimeError(payload["errors"][0].get("message", "Linear query failed"))
    state = ((payload.get("data") or {}).get("issue") or {}).get("state") or {}
    return str(state.get("type") or "").lower()


def atomic_move(source: Path, destination: Path, record: dict, room_state: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    record["room_state"] = room_state
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, destination)
        source.unlink(missing_ok=True)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def reconcile_directory(source_dir: Path, destination_dir: Path, source_is_archive: bool, key: str) -> int:
    changed = 0
    if not source_dir.exists():
        return changed
    for source in sorted(source_dir.glob("*.json")):
        try:
            record = json.loads(source.read_text())
            issue_id = str(record.get("issue_id") or "")
            issue_key = str(record.get("issue_key") or source.stem)
            if not issue_id:
                continue
            completed = issue_state(issue_id, key) == "completed"
            should_move = (not source_is_archive and completed) or (source_is_archive and not completed)
            if should_move:
                atomic_move(source, destination_dir / source.name, record, "archived" if completed else "active")
                print(f"{'archived' if completed else 'reopened'} {issue_key}")
                changed += 1
        except Exception as exc:
            print(f"issue-room lifecycle warning for {source.name}: {exc}", file=sys.stderr)
    return changed


def main() -> int:
    key = api_key()
    if not key:
        print("issue-room lifecycle observer: Linear key unavailable", file=sys.stderr)
        return 2
    ACTIVE.mkdir(parents=True, exist_ok=True)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    reconcile_directory(ACTIVE, ARCHIVE, False, key)
    reconcile_directory(ARCHIVE, ACTIVE, True, key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
