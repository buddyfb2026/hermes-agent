#!/usr/bin/env python3
"""Prepare, verify, and promote Bizina's Hermes fork without touching live on prepare.

The official Desktop updater targets origin/main. Bizina carries a reviewed patch
stack on fork/biab-208-v020-20260819, so updates are integrations, not pulls.
This command keeps candidate creation, verification, staged promotion, runtime
completion, and rollback as separate commit points with an atomic JSON receipt.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_LIVE = Path.home() / ".hermes/hermes-agent"
DEFAULT_CANDIDATES = Path.home() / ".hermes/update-candidates"
DEFAULT_STABLE_BRANCH = "biab-208-v020-20260819"
DEFAULT_UPSTREAM = "origin/main"
DEFAULT_FORK_REMOTE = "fork"


class UpdateError(RuntimeError):
    pass


def run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if check and result.returncode:
        raise UpdateError(f"{' '.join(args)} failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}")
    return result


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd, check)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def branch(repo: Path) -> str:
    return git(repo, "branch", "--show-current").stdout.strip()


def dirty_paths(repo: Path) -> list[str]:
    return [line for line in git(repo, "status", "--porcelain").stdout.splitlines() if line.strip()]


def candidate_receipt(root: Path) -> Path:
    return root.parent / f"{root.name}.receipt.json"


def prepare(args: argparse.Namespace) -> int:
    live = args.live.resolve()
    if branch(live) != args.stable_branch:
        raise UpdateError(f"live branch must be {args.stable_branch}, got {branch(live)}")
    dirty = dirty_paths(live)
    if dirty:
        raise UpdateError("live checkout is dirty: " + ", ".join(dirty))
    if not args.no_fetch:
        git(live, "fetch", "origin", "main", "--quiet")
        git(live, "fetch", args.fork_remote, args.stable_branch, "--quiet")

    stamp = args.stamp or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = (args.candidates / f"bizina-next-{stamp}").resolve()
    candidate_branch = f"bizina-next-{stamp}"
    if candidate.exists():
        raise UpdateError(f"candidate already exists: {candidate}")
    git(live, "worktree", "add", "-b", candidate_branch, str(candidate), f"{args.fork_remote}/{args.stable_branch}")
    before = head(candidate)
    merge = git(candidate, "merge", "--no-commit", "--no-ff", args.upstream, check=False)
    conflicts = git(candidate, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
    status = "conflicts" if conflicts else "prepared"
    if merge.returncode == 0:
        git(candidate, "commit", "-m", f"Merge {args.upstream} into Bizina candidate {stamp}")
    elif not conflicts:
        raise UpdateError(merge.stderr.strip() or merge.stdout.strip() or "merge failed without conflict paths")
    receipt = {
        "schema": 1,
        "status": status,
        "created_at": iso_now(),
        "live_root": str(live),
        "stable_branch": args.stable_branch,
        "stable_head": before,
        "upstream_ref": args.upstream,
        "upstream_head": git(live, "rev-parse", args.upstream).stdout.strip(),
        "candidate_root": str(candidate),
        "candidate_branch": candidate_branch,
        "candidate_head": head(candidate) if not conflicts else None,
        "conflicts": conflicts,
        "verification": [],
    }
    atomic_json(candidate_receipt(candidate), receipt)
    print(json.dumps(receipt, indent=2))
    return 10 if conflicts else 0


def load_receipt(candidate: Path) -> dict[str, Any]:
    path = candidate_receipt(candidate)
    if not path.exists():
        raise UpdateError(f"candidate receipt missing: {path}")
    return json.loads(path.read_text())


def finalize(args: argparse.Namespace) -> int:
    candidate = args.candidate.resolve()
    receipt = load_receipt(candidate)
    if receipt.get("candidate_root") != str(candidate):
        raise UpdateError("receipt candidate_root mismatch")
    conflicts = git(candidate, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
    if conflicts:
        raise UpdateError("unresolved conflicts: " + ", ".join(conflicts))
    merge_head = git(candidate, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).stdout.strip()
    if not merge_head:
        raise UpdateError("candidate is not an in-progress merge")
    git(candidate, "diff", "--cached", "--check")
    git(candidate, "commit", "-m", f"Merge {receipt['upstream_ref']} into Bizina candidate")
    receipt.update(status="prepared", candidate_head=head(candidate), conflicts=[], finalized_at=iso_now())
    atomic_json(candidate_receipt(candidate), receipt)
    print(json.dumps(receipt, indent=2))
    return 0


def verify(args: argparse.Namespace) -> int:
    candidate = args.candidate.resolve()
    receipt = load_receipt(candidate)
    conflicts = git(candidate, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
    if conflicts:
        raise UpdateError("unresolved conflicts: " + ", ".join(conflicts))
    if dirty_paths(candidate):
        raise UpdateError("candidate must be committed and clean before verification")
    checks = [
        ["uvx", "uv@0.9.28", "lock", "--check"],
        ["uv", "sync", "--locked", "--extra", "dev", "--extra", "all"],
        [
            "uv", "run", "--no-sync", "pytest", "-q",
            "tests/gateway/test_hermes_jobs_worker.py",
            "tests/test_bizina_managed_update.py",
            "tests/hermes_cli/test_update_parked_branch_guard.py",
            "tests/hermes_cli/test_update_check.py",
        ],
        ["node", "--check", "apps/desktop/src/plugins/hermes-bots/plugin.js"],
        ["npm", "ci"],
        ["npm", "--workspace", "apps/desktop", "run", "typecheck"],
        ["npm", "--workspace", "apps/desktop", "test"],
        ["hermes", "desktop", "--build-only"],
    ]
    evidence: list[dict[str, Any]] = []
    for command in checks:
        result = subprocess.run(command, cwd=candidate, text=True, capture_output=True)
        evidence.append({"command": " ".join(command), "exit_code": result.returncode, "output_tail": (result.stdout + result.stderr)[-4000:]})
        if result.returncode:
            receipt.update(status="verification_failed", candidate_head=head(candidate), verification=evidence, verified_at=iso_now())
            atomic_json(candidate_receipt(candidate), receipt)
            print(json.dumps(receipt, indent=2))
            return 20
    receipt.update(status="verified", candidate_head=head(candidate), verification=evidence, verified_at=iso_now())
    atomic_json(candidate_receipt(candidate), receipt)
    print(json.dumps({"status": "verified", "candidate": str(candidate), "head": receipt["candidate_head"]}, indent=2))
    return 0


def promote(args: argparse.Namespace) -> int:
    if not args.yes:
        raise UpdateError("promotion requires --yes")
    candidate = args.candidate.resolve()
    receipt = load_receipt(candidate)
    if receipt.get("status") != "verified" or receipt.get("candidate_head") != head(candidate):
        raise UpdateError("candidate receipt is not verified for the current head")
    live = Path(receipt["live_root"])
    if dirty_paths(live):
        raise UpdateError("live checkout is dirty")
    if branch(live) != receipt["stable_branch"]:
        raise UpdateError("live checkout is not on the stable branch")
    previous = head(live)
    tag = "bizina-hermes-pre-update-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    git(live, "tag", tag, previous)
    git(live, "merge", "--ff-only", receipt["candidate_head"])
    receipt.update(
        status="promotion_pending_validation",
        promotion_started_at=iso_now(),
        previous_head=previous,
        rollback_tag=tag,
        promoted_head=head(live),
    )
    atomic_json(candidate_receipt(candidate), receipt)
    print(json.dumps({"status": "promotion_pending_validation", "previous": previous, "head": head(live), "rollback_tag": tag}, indent=2))
    return 0


def complete(args: argparse.Namespace) -> int:
    """Publish a staged promotion only after the wrapper proves runtime health."""
    if not args.yes:
        raise UpdateError("completion requires --yes")
    candidate = args.candidate.resolve()
    receipt = load_receipt(candidate)
    if receipt.get("status") != "promotion_pending_validation":
        raise UpdateError("candidate is not pending runtime validation")
    live = Path(receipt["live_root"])
    if dirty_paths(live):
        raise UpdateError("live checkout became dirty during validation")
    if branch(live) != receipt["stable_branch"]:
        raise UpdateError("live checkout left the stable branch during validation")
    if head(live) != receipt.get("promoted_head") or head(live) != receipt.get("candidate_head"):
        raise UpdateError("live head no longer matches the validated candidate")
    git(live, "push", args.fork_remote, f"HEAD:refs/heads/{receipt['stable_branch']}")
    receipt.update(status="promoted", promoted_at=iso_now(), runtime_validation="passed")
    atomic_json(candidate_receipt(candidate), receipt)
    print(json.dumps({"status": "promoted", "previous": receipt["previous_head"], "head": head(live), "rollback_tag": receipt["rollback_tag"]}, indent=2))
    return 0


def rollback(args: argparse.Namespace) -> int:
    """Restore the exact pre-promotion source head after failed runtime validation."""
    if not args.yes:
        raise UpdateError("rollback requires --yes")
    candidate = args.candidate.resolve()
    receipt = load_receipt(candidate)
    if receipt.get("status") not in {"promotion_pending_validation", "rollback_failed"}:
        raise UpdateError("candidate is not in a rollback-eligible state")
    live = Path(receipt["live_root"])
    previous = str(receipt.get("previous_head") or "")
    promoted = str(receipt.get("promoted_head") or "")
    if not previous or not promoted:
        raise UpdateError("receipt lacks the pre-promotion rollback boundary")
    if dirty_paths(live):
        raise UpdateError("live checkout is dirty; refusing destructive rollback")
    if branch(live) != receipt["stable_branch"]:
        raise UpdateError("live checkout left the stable branch; refusing rollback")
    if head(live) != promoted:
        raise UpdateError("live head changed after promotion; refusing rollback")
    try:
        git(live, "reset", "--hard", previous)
    except UpdateError as exc:
        receipt.update(status="rollback_failed", rollback_failed_at=iso_now(), rollback_error=str(exc))
        atomic_json(candidate_receipt(candidate), receipt)
        raise
    receipt.update(status="rolled_back", rolled_back_at=iso_now(), rollback_reason=args.reason, restored_head=head(live))
    atomic_json(candidate_receipt(candidate), receipt)
    print(json.dumps({"status": "rolled_back", "failed_head": promoted, "restored_head": head(live), "reason": args.reason}, indent=2))
    return 0


def status(args: argparse.Namespace) -> int:
    live = args.live.resolve()
    if not args.no_fetch:
        git(live, "fetch", "origin", "main", "--quiet")
        git(live, "fetch", args.fork_remote, args.stable_branch, "--quiet")
    behind, ahead = git(live, "rev-list", "--left-right", "--count", f"{args.upstream}...HEAD").stdout.split()
    payload = {
        "status": "clean" if not dirty_paths(live) else "dirty",
        "branch": branch(live),
        "head": head(live),
        "upstream": args.upstream,
        "upstream_commits_not_integrated": int(behind),
        "bizina_commits_carried": int(ahead),
    }
    print(json.dumps(payload, indent=2))
    return 0 if payload["status"] == "clean" and payload["branch"] == args.stable_branch else 2


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--live", type=Path, default=DEFAULT_LIVE)
    p.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    p.add_argument("--stable-branch", default=DEFAULT_STABLE_BRANCH)
    p.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    p.add_argument("--fork-remote", default=DEFAULT_FORK_REMOTE)
    p.add_argument("--no-fetch", action="store_true")
    sub = p.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--stamp")
    fin = sub.add_parser("finalize")
    fin.add_argument("candidate", type=Path)
    ver = sub.add_parser("verify")
    ver.add_argument("candidate", type=Path)
    pro = sub.add_parser("promote")
    pro.add_argument("candidate", type=Path)
    pro.add_argument("--yes", action="store_true")
    done = sub.add_parser("complete")
    done.add_argument("candidate", type=Path)
    done.add_argument("--yes", action="store_true")
    undo = sub.add_parser("rollback")
    undo.add_argument("candidate", type=Path)
    undo.add_argument("--reason", required=True)
    undo.add_argument("--yes", action="store_true")
    sub.add_parser("status")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return {
            "prepare": prepare,
            "finalize": finalize,
            "verify": verify,
            "promote": promote,
            "complete": complete,
            "rollback": rollback,
            "status": status,
        }[args.action](args)
    except UpdateError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
