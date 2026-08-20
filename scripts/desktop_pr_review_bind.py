#!/usr/bin/env python3
"""Create explicit Hermes Desktop PR-review bindings safely.

Claude Desktop may be discovered only from its structured prs[] metadata with an
exact PR URL/number match. ChatGPT Desktop always requires an explicit thread id
and is verified against the read-only Codex thread store. Titles, CWDs, branch
names, and transcript text are never routing evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

ISSUE_RE = re.compile(r"^[A-Z]{2,10}-\d+$")
PR_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/(\d+)$")


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def validate_identity(issue_key: str, pr_url: str, pr_number: int) -> tuple[str, str, int]:
    issue = issue_key.strip().upper()
    url = pr_url.strip()
    match = PR_RE.fullmatch(url)
    if not ISSUE_RE.fullmatch(issue):
        raise ValueError("invalid issue key")
    if not match or int(match.group(1)) != pr_number:
        raise ValueError("PR URL and number do not match")
    return issue, url, pr_number


def claude_candidates(meta_root: Path, pr_url: str, pr_number: int) -> list[str]:
    found: set[str] = set()
    for path in meta_root.glob("**/*.json"):
        try:
            record = json.loads(path.read_text())
        except Exception:
            continue
        prs = record.get("prs") if isinstance(record.get("prs"), list) else []
        if not any(
            isinstance(pr, dict)
            and str(pr.get("url") or "") == pr_url
            and int(pr.get("prNumber") or 0) == pr_number
            for pr in prs
        ):
            continue
        thread = str(record.get("sessionId") or record.get("cliSessionId") or "").strip()
        if thread:
            found.add(thread)
    return sorted(found)


def verify_chatgpt_thread(db_path: Path, thread_id: str) -> None:
    if not db_path.exists():
        raise ValueError("ChatGPT Desktop thread store unavailable")
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        row = db.execute("SELECT rollout_path FROM threads WHERE id=?", (thread_id,)).fetchone()
    if not row or not row[0]:
        raise ValueError("ChatGPT Desktop thread not found")
    originator = ""
    for raw_line in Path(row[0]).read_text(errors="ignore").splitlines():
        try:
            event = json.loads(raw_line)
            if event.get("type") == "session_meta":
                originator = str((event.get("payload") or {}).get("originator") or "")
                break
        except Exception:
            continue
    if originator != "Codex Desktop":
        raise ValueError("thread is not owned by ChatGPT/Codex Desktop")


def binding_name(provider: str, issue: str, pr_number: int, thread: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", thread).strip("-")[:48] or "thread"
    return f"{issue}-pr{pr_number}-{provider}-{safe}.json"


def main(argv: list[str] | None = None) -> int:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=("claude-desktop", "chatgpt-desktop"))
    parser.add_argument("--issue-key", required=True)
    parser.add_argument("--pr-url", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--thread-id")
    parser.add_argument("--discover", action="store_true", help="Discover one Claude thread from exact structured PR metadata")
    parser.add_argument("--bindings-root", type=Path, default=home / ".hermes/external-workers/pr-reviews/bindings")
    parser.add_argument("--claude-meta-root", type=Path, default=home / "Library/Application Support/Claude/claude-code-sessions")
    parser.add_argument("--codex-db", type=Path, default=home / ".codex/state_5.sqlite")
    args = parser.parse_args(argv)

    issue, url, number = validate_identity(args.issue_key, args.pr_url, args.pr_number)
    thread = str(args.thread_id or "").strip()
    if args.provider == "claude-desktop" and args.discover:
        candidates = claude_candidates(args.claude_meta_root, url, number)
        if len(candidates) != 1:
            raise ValueError(f"expected exactly one Claude Desktop thread for PR #{number}; found {len(candidates)}")
        thread = candidates[0]
    elif args.discover:
        raise ValueError("ChatGPT Desktop discovery is forbidden; pass an explicit --thread-id")

    if not thread:
        raise ValueError("explicit --thread-id required")
    if args.provider == "chatgpt-desktop":
        verify_chatgpt_thread(args.codex_db, thread)

    payload = {
        "schema": 1,
        "provider": args.provider,
        "provider_thread_id": thread,
        "issue_key": issue,
        "pr_url": url,
        "pr_number": number,
        "review_id": thread,
    }
    destination = args.bindings_root / binding_name(args.provider, issue, number, thread)
    atomic_write(destination, payload)
    print(json.dumps({"ok": True, "binding": str(destination), "provider_thread_id": thread}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
