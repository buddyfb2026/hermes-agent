import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bizina-managed_update.py"
SPEC = importlib.util.spec_from_file_location("bizina_managed_update", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def cmd(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def commit(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(body)
    subprocess.check_call(["git", "add", name], cwd=repo)
    subprocess.check_call(["git", "commit", "-m", body.strip()], cwd=repo, stdout=subprocess.DEVNULL)
    return cmd(repo, "rev-parse", "HEAD")


def repo(tmp_path: Path, conflict: bool = False) -> Path:
    root = tmp_path / "live"
    root.mkdir()
    cmd(root, "init")
    cmd(root, "config", "user.email", "test@example.com")
    cmd(root, "config", "user.name", "Test")
    commit(root, "shared.txt", "base\n")
    cmd(root, "branch", "-M", "stable")
    base = cmd(root, "rev-parse", "HEAD")

    cmd(root, "checkout", "-b", "upstream-work", base)
    commit(root, "shared.txt" if conflict else "upstream.txt", "upstream\n")
    upstream = cmd(root, "rev-parse", "HEAD")

    cmd(root, "checkout", "stable")
    commit(root, "shared.txt" if conflict else "bizina.txt", "bizina\n")
    stable = cmd(root, "rev-parse", "HEAD")
    cmd(root, "update-ref", "refs/remotes/origin/main", upstream)
    cmd(root, "update-ref", "refs/remotes/fork/stable", stable)
    return root


def args(root: Path, candidates: Path, stamp: str = "test") -> SimpleNamespace:
    return SimpleNamespace(
        live=root,
        candidates=candidates,
        stable_branch="stable",
        upstream="origin/main",
        fork_remote="fork",
        no_fetch=True,
        stamp=stamp,
    )


def test_prepare_clean_merge_writes_external_receipt(tmp_path):
    root = repo(tmp_path)
    candidates = tmp_path / "candidates"
    assert MODULE.prepare(args(root, candidates)) == 0
    candidate = candidates / "bizina-next-test"
    receipt = candidates / "bizina-next-test.receipt.json"
    assert candidate.exists() and receipt.exists()
    assert not (candidate / "bizina-update-receipt.json").exists()
    record = json.loads(receipt.read_text())
    assert record["status"] == "prepared"
    assert record["candidate_head"] == cmd(candidate, "rev-parse", "HEAD")
    assert cmd(candidate, "status", "--porcelain") == ""


def test_prepare_conflict_retains_candidate_and_lists_paths(tmp_path):
    root = repo(tmp_path, conflict=True)
    candidates = tmp_path / "candidates"
    assert MODULE.prepare(args(root, candidates)) == 10
    record = json.loads((candidates / "bizina-next-test.receipt.json").read_text())
    assert record["status"] == "conflicts"
    assert record["conflicts"] == ["shared.txt"]
    assert record["candidate_head"] is None


def test_finalize_commits_resolved_merge_and_refreshes_receipt(tmp_path):
    root = repo(tmp_path, conflict=True)
    candidates = tmp_path / "candidates"
    assert MODULE.prepare(args(root, candidates)) == 10
    candidate = candidates / "bizina-next-test"
    with pytest.raises(MODULE.UpdateError, match="unresolved conflicts"):
        MODULE.finalize(SimpleNamespace(candidate=candidate))
    (candidate / "shared.txt").write_text("resolved\n")
    cmd(candidate, "add", "shared.txt")
    assert MODULE.finalize(SimpleNamespace(candidate=candidate)) == 0
    record = json.loads((candidates / "bizina-next-test.receipt.json").read_text())
    assert record["status"] == "prepared"
    assert record["conflicts"] == []
    assert record["candidate_head"] == cmd(candidate, "rev-parse", "HEAD")
    assert cmd(candidate, "status", "--porcelain") == ""


def test_promote_requires_explicit_yes(tmp_path):
    root = repo(tmp_path)
    candidates = tmp_path / "candidates"
    assert MODULE.prepare(args(root, candidates)) == 0
    candidate = candidates / "bizina-next-test"
    with pytest.raises(MODULE.UpdateError, match="requires --yes"):
        MODULE.promote(SimpleNamespace(candidate=candidate, yes=False, fork_remote="fork"))


def test_atomic_json_is_mode_0600(tmp_path):
    path = tmp_path / "receipt.json"
    MODULE.atomic_json(path, {"status": "ok"})
    assert json.loads(path.read_text()) == {"status": "ok"}
    assert path.stat().st_mode & 0o777 == 0o600
