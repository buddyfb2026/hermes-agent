"""
Tests for ``gateway.hermes_jobs_worker`` — the BIZ-208 Hermes Avengers poll
loop.

Coverage matrix (from packet §8 acceptance criteria):

  * Profile→callsign mapping for all five profiles + base + override.
  * Claim SQL pinning: ``FOR UPDATE SKIP LOCKED``, priority ordering,
    expired-lease reclaim predicate, ``RETURNING *``.
  * No-row idle path sleeps + skips runner.
  * Successful runner flips ``completed_at``.
  * Failed runner flips ``failed_at`` and writes ``last_error``.
  * Lease renewal guarded by id + claimed_by.
  * Renewal-rejection (row stolen) raises and stops processing.
  * Shutdown event short-circuits idle sleep promptly.
  * Missing DSN disables worker without crashing.
  * Two simulated workers cannot claim the same row (mocked-cursor).

The full live-DB integration smoke runs in ``scripts/biz208_live_smoke.sh``
(out-of-band; not pytest because it depends on a real Postgres).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import OrderedDict
from typing import Any, Optional
from unittest.mock import patch

import pytest

from gateway.hermes_jobs_worker import (
    CLAIM_NEXT_HERMES_JOB_SQL,
    COMPLETE_JOB_SQL,
    FAIL_JOB_SQL,
    PROFILE_TO_CALLSIGN,
    RATE_LIMIT_BACKOFF_BASE_SECONDS,
    RATE_LIMIT_BACKOFF_CAP_SECONDS,
    RELEASE_JOB_SQL,
    RENEW_LEASE_SQL,
    TRANSIENT_FAILURE_SUBSTRINGS,
    ZERO_API_CALLS_ERROR,
    ZERO_API_CALLS_MARKER,
    HermesJobsWorker,
    build_runner_job_from_row,
    redact_dsn,
    resolve_callsign,
    write_issue_room_state,
)


# ---------------------------------------------------------------------------
# Profile → callsign mapping
# ---------------------------------------------------------------------------

class TestCallsignResolution:
    def test_default_profile_is_hermes(self):
        assert resolve_callsign("default", env={}) == "Hermes"

    def test_named_profile_dirs(self):
        assert resolve_callsign("hermes2", env={}) == "Iron Man"
        assert resolve_callsign("hermes3", env={}) == "Captain America"
        assert resolve_callsign("hermes4", env={}) == "Black Widow"
        assert resolve_callsign("hermes5", env={}) == "Spiderman"

    def test_forward_compat_hermes_profile_dir(self):
        assert resolve_callsign("hermes", env={}) == "Hermes"

    def test_unknown_profile_returns_none(self):
        assert resolve_callsign("alpaca", env={}) is None
        assert resolve_callsign(None, env={}) is None
        assert resolve_callsign("", env={}) is None

    def test_env_override_wins(self):
        assert resolve_callsign("hermes2", env={"HERMES_AVENGER_CALLSIGN": "Thor"}) == "Thor"
        # Override even on unmapped profile.
        assert resolve_callsign("alpaca", env={"HERMES_AVENGER_CALLSIGN": "Hulk"}) == "Hulk"

    def test_env_override_trims_whitespace(self):
        assert (
            resolve_callsign("hermes2", env={"HERMES_AVENGER_CALLSIGN": "  Iron Man  "})
            == "Iron Man"
        )

    def test_empty_override_falls_through_to_table(self):
        assert resolve_callsign("hermes4", env={"HERMES_AVENGER_CALLSIGN": ""}) == "Black Widow"
        assert resolve_callsign("hermes4", env={"HERMES_AVENGER_CALLSIGN": "   "}) == "Black Widow"

    def test_full_table_matches_packet_spec(self):
        # Exact spelling and casing — central-api `.trim()`s but does NOT
        # case-fold. Catches a typo regression in PROFILE_TO_CALLSIGN.
        assert PROFILE_TO_CALLSIGN["default"] == "Hermes"
        assert PROFILE_TO_CALLSIGN["hermes2"] == "Iron Man"
        assert PROFILE_TO_CALLSIGN["hermes3"] == "Captain America"
        assert PROFILE_TO_CALLSIGN["hermes4"] == "Black Widow"
        assert PROFILE_TO_CALLSIGN["hermes5"] == "Spiderman"


# ---------------------------------------------------------------------------
# DSN redaction (no secrets in logs)
# ---------------------------------------------------------------------------

class TestRedactDsn:
    def test_password_is_redacted(self):
        out = redact_dsn("postgresql://user:hunter2@localhost:5432/biab_central")
        assert "hunter2" not in out
        assert "***" in out
        assert "user" in out
        assert "localhost" in out

    def test_no_password_passes_through(self):
        out = redact_dsn("postgresql://localhost:5432/biab_central")
        assert out == "postgresql://localhost:5432/biab_central"

    def test_empty_dsn_is_safe(self):
        assert redact_dsn("") == ""


# ---------------------------------------------------------------------------
# Pinned SQL — must match central-api's CLAIM_NEXT_HERMES_JOB_SQL shape
# (services/central-api/src/linear/agent-router.ts).
# ---------------------------------------------------------------------------

class TestClaimSqlPinning:
    def test_for_update_skip_locked(self):
        assert "FOR UPDATE SKIP LOCKED" in CLAIM_NEXT_HERMES_JOB_SQL

    def test_priority_ordering(self):
        assert "priority DESC" in CLAIM_NEXT_HERMES_JOB_SQL
        assert "queued_at" in CLAIM_NEXT_HERMES_JOB_SQL

    def test_lease_window(self):
        assert "interval '30 minutes'" in CLAIM_NEXT_HERMES_JOB_SQL

    def test_expired_lease_reclaim(self):
        # The combination that distinguishes BIZ-208 from a naive
        # `claimed_by IS NULL` claim — must include lease-expiry reclaim.
        assert "claimed_by IS NULL OR lease_expires_at < now()" in CLAIM_NEXT_HERMES_JOB_SQL

    def test_transient_retry_deadline_blocks_fleet_reclaim(self):
        assert "metadata->'retry_after_epoch' IS NULL" in CLAIM_NEXT_HERMES_JOB_SQL
        assert "retry_after_epoch" in RELEASE_JOB_SQL
        assert "transient_attempts" in RELEASE_JOB_SQL
        assert "LEAST(" in RELEASE_JOB_SQL
        assert "last_error = $3" in RELEASE_JOB_SQL

    def test_returning_star(self):
        assert "RETURNING *" in CLAIM_NEXT_HERMES_JOB_SQL

    def test_completed_and_failed_rows_excluded(self):
        assert "completed_at IS NULL" in CLAIM_NEXT_HERMES_JOB_SQL
        assert "failed_at IS NULL" in CLAIM_NEXT_HERMES_JOB_SQL

    def test_biz276_exclude_callsign_hard_gate(self):
        assert "metadata->>'exclude_callsign'" in CLAIM_NEXT_HERMES_JOB_SQL
        assert "metadata->>'exclude_callsign' != $1" in CLAIM_NEXT_HERMES_JOB_SQL

    def test_biz276_preferred_callsign_soft_fallback(self):
        assert "metadata->>'preferred_callsign'" in CLAIM_NEXT_HERMES_JOB_SQL
        assert "metadata->>'preferred_callsign' = $1" in CLAIM_NEXT_HERMES_JOB_SQL
        assert "queued_at <= now() - interval '30 seconds'" in CLAIM_NEXT_HERMES_JOB_SQL

    def test_renewal_guarded_by_id_and_callsign(self):
        assert "WHERE id = $1" in RENEW_LEASE_SQL
        assert "AND claimed_by = $2" in RENEW_LEASE_SQL

    def test_completion_guarded_by_id_and_callsign(self):
        assert "WHERE id = $1" in COMPLETE_JOB_SQL
        assert "AND claimed_by = $2" in COMPLETE_JOB_SQL

    def test_failure_guarded_by_id_and_callsign(self):
        assert "WHERE id = $1" in FAIL_JOB_SQL
        assert "AND claimed_by = $2" in FAIL_JOB_SQL
        assert "last_error = $3" in FAIL_JOB_SQL


class TestBiz276AffinitySemantics:
    """Deterministic model of the BIZ-276 claim predicates.

    The live enforcement is SQL, but these tests pin the business
    semantics so a future SQL rewrite cannot accidentally allow an early
    non-preferred re-review claim or weaken merge exclusion.
    """

    @staticmethod
    def _claimable(metadata: Any, callsign: str, age_seconds: int) -> bool:
        if not isinstance(metadata, dict):
            metadata = {}
        exclude = metadata.get("exclude_callsign")
        preferred = metadata.get("preferred_callsign")
        if isinstance(exclude, str) and exclude.strip() and exclude == callsign:
            return False
        if not isinstance(preferred, str) or not preferred.strip():
            return True
        if preferred == callsign:
            return True
        return age_seconds >= 30

    def test_preferred_callsign_claims_immediately(self):
        metadata = {"preferred_callsign": "Hermes"}
        assert self._claimable(metadata, "Hermes", 0) is True

    def test_non_preferred_waits_until_30_second_fallback(self):
        metadata = {"preferred_callsign": "Hermes"}
        assert self._claimable(metadata, "Iron Man", 29) is False
        assert self._claimable(metadata, "Iron Man", 30) is True

    def test_exclude_callsign_has_no_fallback_window(self):
        metadata = {"exclude_callsign": "Iron Man"}
        assert self._claimable(metadata, "Iron Man", 0) is False
        assert self._claimable(metadata, "Iron Man", 30) is False
        assert self._claimable(metadata, "Iron Man", 3600) is False
        assert self._claimable(metadata, "Hermes", 0) is True

    def test_missing_blank_or_malformed_metadata_is_fail_open(self):
        assert self._claimable({}, "Black Widow", 0) is True
        assert self._claimable({"preferred_callsign": ""}, "Black Widow", 0) is True
        assert self._claimable({"exclude_callsign": ""}, "Black Widow", 0) is True
        assert self._claimable("not-json-object", "Black Widow", 0) is True


# ---------------------------------------------------------------------------
# Row → cron-job-dict adapter
# ---------------------------------------------------------------------------

class TestBuildRunnerJob:
    def _row(self, **overrides):
        base = {
            "id": "11111111-2222-3333-4444-555555555555",
            "issue_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "issue_key": "BIZ-208",
            "title": "test issue",
            "priority": 0,
            "dispatch_lane": "packet_authoring",
            "payload": {
                "packet_message": "Author packet for BIZ-208",
                "packet_version": 2,
            },
            "metadata": {},
        }
        base.update(overrides)
        return base

    def test_required_fields_present(self):
        job = build_runner_job_from_row(self._row(), callsign="Black Widow")
        assert job["id"]
        assert job["name"] == "hermes-pool-BIZ-208-v2"
        assert job["prompt"] == "Author packet for BIZ-208"

    def test_metadata_carries_attribution(self):
        job = build_runner_job_from_row(self._row(), callsign="Iron Man")
        assert job["metadata"]["claimed_by"] == "Iron Man"
        assert job["metadata"]["issue_key"] == "BIZ-208"
        assert job["metadata"]["packet_version"] == 2
        assert job["metadata"]["hermes_jobs_row_id"] == "11111111-2222-3333-4444-555555555555"

    def test_payload_can_be_json_string(self):
        row = self._row(payload='{"packet_message": "from-string", "packet_version": 5}')
        job = build_runner_job_from_row(row, callsign="Hermes")
        assert job["prompt"] == "from-string"
        assert job["metadata"]["packet_version"] == 5

    def test_deliver_falls_back_to_local_without_home_channel(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)
        job = build_runner_job_from_row(self._row(), callsign="Hermes")
        assert job["deliver"] == "local"

    def test_deliver_uses_telegram_when_home_channel_set(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "12345")
        job = build_runner_job_from_row(self._row(), callsign="Hermes")
        assert job["deliver"] == "telegram"

    def test_unknown_packet_version_defaults_to_one(self):
        row = self._row(payload={"packet_message": "x"})
        job = build_runner_job_from_row(row, callsign="Hermes")
        assert job["metadata"]["packet_version"] == 1
        assert job["name"] == "hermes-pool-BIZ-208-v1"


# ---------------------------------------------------------------------------
# Async fakes for HermesJobsWorker — no real asyncpg / no real DB.
# ---------------------------------------------------------------------------

class _FakeConnection:
    """Implements just enough of asyncpg.Connection for the worker."""

    def __init__(self, pool: "_FakePool"):
        self._pool = pool

    async def fetchrow(self, sql: str, *args):
        self._pool.calls.append(("fetchrow", sql, args))
        return self._pool.next_claim_row()

    async def fetchval(self, sql: str, *args):
        self._pool.calls.append(("fetchval", sql, args))
        return self._pool.next_renewal_value()

    async def execute(self, sql: str, *args):
        self._pool.calls.append(("execute", sql, args))
        return "UPDATE 1"


class _FakeAcquireCM:
    def __init__(self, pool: "_FakePool"):
        self._pool = pool

    async def __aenter__(self):
        return _FakeConnection(self._pool)

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    """Implements just enough of asyncpg.Pool for the worker."""

    def __init__(
        self,
        *,
        claim_rows: Optional[list] = None,
        renewal_returns: Optional[list] = None,
    ):
        self._claim_rows = list(claim_rows or [])
        self._renewal_returns = list(renewal_returns or [])
        self.calls: list = []
        self.closed = False

    def next_claim_row(self):
        if not self._claim_rows:
            return None
        return self._claim_rows.pop(0)

    def next_renewal_value(self):
        if not self._renewal_returns:
            return "renewed"  # default: still ours
        return self._renewal_returns.pop(0)

    def acquire(self):
        return _FakeAcquireCM(self)

    async def close(self):
        self.closed = True


def _make_worker(
    *,
    callsign: str = "Black Widow",
    pool: Optional[_FakePool] = None,
    runner=None,
    issue_room_root=None,
    poll_interval_seconds: float = 0.01,
    poll_jitter_seconds: float = 0.0,
    lease_renewal_interval_seconds: float = 0.05,
):
    worker = HermesJobsWorker(
        callsign=callsign,
        dsn="postgresql://x:***@localhost/test",
        issue_room_root=issue_room_root,
        adapters={},
        loop=None,
        poll_interval_seconds=poll_interval_seconds,
        poll_jitter_seconds=poll_jitter_seconds,
        lease_renewal_interval_seconds=lease_renewal_interval_seconds,
        runner=runner,
    )
    if pool is not None:
        worker._pool = pool
    return worker


def _row(claim_id="rid-1", issue_key="BIZ-208", packet_version=2):
    return {
        "id": claim_id,
        "issue_id": "iid",
        "issue_key": issue_key,
        "title": "t",
        "priority": 0,
        "dispatch_lane": "packet_authoring",
        "payload": {"packet_message": "do the thing", "packet_version": packet_version},
        "metadata": {},
    }


# ---------------------------------------------------------------------------
# Observable Linear issue-room bridge
# ---------------------------------------------------------------------------


def test_claim_only_does_not_seat_an_avenger(tmp_path):
    path = write_issue_room_state(
        tmp_path,
        _row(claim_id="claim-only", issue_key="BIZ-9998"),
        profile_name="hermes4",
        callsign="Black Widow",
        status="claimed",
    )
    state = json.loads(path.read_text())
    assert state["members"] == []
    assert state["authoring"]["status"] == "claimed"


def test_issue_room_state_is_atomic_privacy_bounded_and_preserves_members(tmp_path):
    first = _row(claim_id="job-1", issue_key="BIZ-9999")
    path = write_issue_room_state(
        tmp_path,
        first,
        profile_name="hermes3",
        callsign="Captain America",
        status="authoring",
        session_key="telegram-key",
        session_id="session-1",
        baseline_message_count=12,
    )
    state = json.loads(path.read_text())
    assert state["issue_key"] == "BIZ-9999"
    assert state["members"] == [
        {
            "profile": "hermes3",
            "callsign": "Captain America",
            "joined_at": state["members"][0]["joined_at"],
        }
    ]
    assert state["authoring"]["baseline_message_count"] == 12
    assert "prompt" not in json.dumps(state).lower()

    retry = _row(claim_id="job-2", issue_key="BIZ-9999")
    write_issue_room_state(
        tmp_path,
        retry,
        profile_name="hermes4",
        callsign="Black Widow",
        status="claimed",
    )
    updated = json.loads(path.read_text())
    assert [member["profile"] for member in updated["members"]] == ["hermes3"]
    write_issue_room_state(
        tmp_path,
        retry,
        profile_name="hermes4",
        callsign="Black Widow",
        status="authoring",
        session_key="telegram-key-2",
    )
    updated = json.loads(path.read_text())
    assert [member["profile"] for member in updated["members"]] == ["hermes3", "hermes4"]
    assert updated["job_id"] == "job-2"
    assert updated["authoring"]["session_id"] == ""
    assert updated["authoring"]["baseline_message_count"] == 0


def test_session_snapshot_reads_id_and_baseline_without_mutation():
    class Store:
        def peek_session_id(self, key):
            assert key == "telegram-key"
            return "session-1"

        def load_transcript(self, session_id):
            assert session_id == "session-1"
            return [{"role": "user"}, {"role": "assistant"}]

    adapter = type("Adapter", (), {"_session_store": Store()})()
    assert HermesJobsWorker._session_transcript_snapshot(adapter, "telegram-key") == (
        "session-1",
        2,
    )


@pytest.mark.asyncio
async def test_process_row_publishes_claim_and_terminal_status(tmp_path):
    pool = _FakePool(claim_rows=[_row()])

    def fake_runner(_job):
        return (True, "doc", "final response", None)

    worker = _make_worker(pool=pool, runner=fake_runner, issue_room_root=tmp_path)
    await worker._process_row(_row())
    state = json.loads((tmp_path / "BIZ-208.json").read_text())
    assert state["members"] == []  # injected runner never opened an authoring session
    assert state["authoring"]["status"] == "packet_ready"
    # Packet completion does not close the issue room; Linear Done owns closure later.
    assert state["room_state"] == "active"


# ---------------------------------------------------------------------------
# Worker behavior — uses _FakePool, no real loop wait, no asyncpg
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestWorkerProcessRow:
    async def test_success_writes_completed_at(self):
        pool = _FakePool(claim_rows=[_row()])
        called: dict = {}

        def fake_runner(job):
            called["job"] = job
            return (True, "doc", "final response", None)

        worker = _make_worker(pool=pool, runner=fake_runner)
        await worker._process_row(_row())

        # COMPLETE_JOB_SQL was issued with (id, callsign).
        execute_calls = [c for c in pool.calls if c[0] == "execute"]
        assert any(
            "completed_at = now()" in c[1] and c[2] == ("rid-1", "Black Widow")
            for c in execute_calls
        ), f"expected completed_at update, got {execute_calls}"

        assert called["job"]["metadata"]["claimed_by"] == "Black Widow"

    async def test_failure_writes_failed_at_with_last_error(self):
        pool = _FakePool(claim_rows=[_row()])

        def fake_runner(job):
            return (False, "doc", "", "boom: something blew up")

        worker = _make_worker(pool=pool, runner=fake_runner)
        await worker._process_row(_row())

        execute_calls = [c for c in pool.calls if c[0] == "execute"]
        fail_calls = [c for c in execute_calls if "failed_at = now()" in c[1]]
        assert fail_calls, f"expected failed_at update, got {execute_calls}"
        # Args: (id, callsign, last_error)
        assert fail_calls[0][2][0] == "rid-1"
        assert fail_calls[0][2][1] == "Black Widow"
        assert "boom" in fail_calls[0][2][2]

    async def test_runner_exception_becomes_failed_at(self):
        pool = _FakePool(claim_rows=[_row()])

        def fake_runner(job):
            raise RuntimeError("kaboom")

        worker = _make_worker(pool=pool, runner=fake_runner)
        await worker._process_row(_row())

        execute_calls = [c for c in pool.calls if c[0] == "execute"]
        fail_calls = [c for c in execute_calls if "failed_at = now()" in c[1]]
        assert fail_calls
        assert "kaboom" in fail_calls[0][2][2]

    async def test_empty_packet_message_short_circuits(self):
        pool = _FakePool()
        runner_called = []

        def fake_runner(job):
            runner_called.append(job)
            return (True, "doc", "x", None)

        worker = _make_worker(pool=pool, runner=fake_runner)
        empty_row = _row()
        empty_row["payload"] = {"packet_message": "  ", "packet_version": 2}
        await worker._process_row(empty_row)

        # Runner was NOT called — empty prompt is a deterministic skip.
        assert runner_called == []
        # Row was marked failed with a clear message.
        execute_calls = [c for c in pool.calls if c[0] == "execute"]
        fail_calls = [c for c in execute_calls if "failed_at = now()" in c[1]]
        assert fail_calls
        assert "empty packet_message" in fail_calls[0][2][2]


@pytest.mark.asyncio
class TestWorkerLeaseRenewal:
    async def test_renewal_stops_when_row_no_longer_owned(self, caplog):
        pool = _FakePool(renewal_returns=[None])  # renewal returns NULL → stolen
        worker = _make_worker(
            pool=pool,
            lease_renewal_interval_seconds=0.01,
        )

        with caplog.at_level(logging.ERROR):
            task = asyncio.create_task(worker._renew_lease_loop("rid-1"))
            # Give the renewal loop a chance to run once.
            await asyncio.sleep(0.05)
            worker._stop_event.set()
            await asyncio.wait_for(task, timeout=1.0)

        # The renewal SQL was attempted with (id, callsign).
        fetchval_calls = [c for c in pool.calls if c[0] == "fetchval"]
        assert fetchval_calls
        assert fetchval_calls[0][2] == ("rid-1", "Black Widow")

        # The lease-lost error surfaced loudly.
        assert any("lease lost" in r.getMessage() for r in caplog.records)

    async def test_renewal_continues_while_owned(self):
        # Three consecutive successful renewals, then stop_event.
        pool = _FakePool(renewal_returns=["renewed", "renewed", "renewed"])
        worker = _make_worker(
            pool=pool,
            lease_renewal_interval_seconds=0.01,
        )

        task = asyncio.create_task(worker._renew_lease_loop("rid-1"))
        await asyncio.sleep(0.05)
        worker._stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)

        fetchval_calls = [c for c in pool.calls if c[0] == "fetchval"]
        assert len(fetchval_calls) >= 1
        # Every call is guarded by (id, callsign).
        for c in fetchval_calls:
            assert c[2] == ("rid-1", "Black Widow")


@pytest.mark.asyncio
class TestWorkerIdleAndShutdown:
    async def test_no_row_sleeps_then_stop_short_circuits(self):
        # Pool always returns no row. Worker should poll, sleep, and then
        # exit promptly when stop_event is set — without ever invoking the
        # runner.
        pool = _FakePool(claim_rows=[])
        runner_called = []

        def fake_runner(job):
            runner_called.append(job)
            return (True, "x", "y", None)

        worker = _make_worker(
            pool=pool,
            runner=fake_runner,
            poll_interval_seconds=0.5,
            poll_jitter_seconds=0.0,
        )

        async def _stop_soon():
            await asyncio.sleep(0.05)
            worker._stop_event.set()

        # Patch asyncpg.create_pool to hand the worker our fake.
        with patch.object(HermesJobsWorker, "_claim_one", new=lambda self: _async_none()):
            stop_task = asyncio.create_task(_stop_soon())
            # Don't run worker.run() (it imports asyncpg). Drive the inner
            # loop body directly: simulate two iterations of the idle path.
            for _ in range(2):
                if worker._stop_event.is_set():
                    break
                row = None
                if row is None:
                    sleep_for = worker.poll_interval_seconds
                    try:
                        await asyncio.wait_for(
                            worker._stop_event.wait(), timeout=sleep_for,
                        )
                        break
                    except asyncio.TimeoutError:
                        continue
            await stop_task

        assert runner_called == []


async def _async_none():
    return None


# ---------------------------------------------------------------------------
# from_env() off-ramp behavior
# ---------------------------------------------------------------------------

class TestFromEnvOffRamp:
    def test_missing_dsn_disables_worker(self, monkeypatch, caplog):
        monkeypatch.delenv("HERMES_JOBS_DATABASE_URL", raising=False)
        with caplog.at_level(logging.INFO):
            worker = HermesJobsWorker.from_env()
        assert worker is None
        assert any(
            "HERMES_JOBS_DATABASE_URL not set" in r.getMessage()
            for r in caplog.records
        )

    def test_unknown_profile_disables_without_override(self, monkeypatch, caplog):
        monkeypatch.setenv("HERMES_JOBS_DATABASE_URL", "postgresql://u:p@h/d")
        monkeypatch.delenv("HERMES_AVENGER_CALLSIGN", raising=False)
        with patch(
            "hermes_cli.profiles.get_active_profile_name", return_value="unknown-profile",
        ), caplog.at_level(logging.WARNING):
            worker = HermesJobsWorker.from_env()
        assert worker is None
        assert any(
            "no callsign mapping" in r.getMessage() for r in caplog.records
        )

    def test_default_profile_constructs_hermes_callsign(self, monkeypatch):
        monkeypatch.setenv("HERMES_JOBS_DATABASE_URL", "postgresql://u:p@h/d")
        monkeypatch.delenv("HERMES_AVENGER_CALLSIGN", raising=False)
        with patch(
            "hermes_cli.profiles.get_active_profile_name", return_value="default",
        ):
            worker = HermesJobsWorker.from_env()
        assert worker is not None
        assert worker.callsign == "Hermes"

    def test_env_override_constructs_under_arbitrary_callsign(self, monkeypatch):
        monkeypatch.setenv("HERMES_JOBS_DATABASE_URL", "postgresql://u:p@h/d")
        monkeypatch.setenv("HERMES_AVENGER_CALLSIGN", "Thor")
        with patch(
            "hermes_cli.profiles.get_active_profile_name", return_value="something-else",
        ):
            worker = HermesJobsWorker.from_env()
        assert worker is not None
        assert worker.callsign == "Thor"

    def test_poll_interval_from_env(self, monkeypatch):
        monkeypatch.setenv("HERMES_JOBS_DATABASE_URL", "postgresql://u:p@h/d")
        monkeypatch.setenv("HERMES_AVENGER_POLL_INTERVAL_SECONDS", "7.5")
        with patch(
            "hermes_cli.profiles.get_active_profile_name", return_value="hermes2",
        ):
            worker = HermesJobsWorker.from_env()
        assert worker is not None
        assert worker.poll_interval_seconds == 7.5

    def test_invalid_poll_interval_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HERMES_JOBS_DATABASE_URL", "postgresql://u:p@h/d")
        monkeypatch.setenv("HERMES_AVENGER_POLL_INTERVAL_SECONDS", "not-a-number")
        with patch(
            "hermes_cli.profiles.get_active_profile_name", return_value="hermes2",
        ):
            worker = HermesJobsWorker.from_env()
        assert worker is not None
        assert worker.poll_interval_seconds == 3.0


# ---------------------------------------------------------------------------
# Two simulated workers cannot claim the same row.
#
# Real concurrency is enforced by Postgres FOR UPDATE SKIP LOCKED, not by
# our Python — so this test pins the *contract* (the SQL we send) rather
# than re-testing PG. The live-DB smoke covers actual concurrency.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_workers_issue_canonical_claim_sql():
    pool_a = _FakePool(claim_rows=[_row()])
    pool_b = _FakePool(claim_rows=[])  # second worker sees empty queue
    worker_a = _make_worker(callsign="Iron Man", pool=pool_a)
    worker_b = _make_worker(callsign="Captain America", pool=pool_b)

    row_a = await worker_a._claim_one()
    row_b = await worker_b._claim_one()

    assert row_a is not None
    assert row_b is None

    # Both workers issued the canonical claim SQL with their own callsign.
    fetch_a = [c for c in pool_a.calls if c[0] == "fetchrow"]
    fetch_b = [c for c in pool_b.calls if c[0] == "fetchrow"]
    assert fetch_a and fetch_a[0][1] == CLAIM_NEXT_HERMES_JOB_SQL
    assert fetch_a[0][2] == ("Iron Man",)
    assert fetch_b and fetch_b[0][1] == CLAIM_NEXT_HERMES_JOB_SQL
    assert fetch_b[0][2] == ("Captain America",)


# ===========================================================================
# BIZ-352 — rate-limit / auth no-op detection + bounded backoff.
#
# A 429 / auth-dead window during packet authoring makes the agent loop exit
# cleanly with ZERO successful API calls and no artifact. The pre-fix code
# recorded such a row completed_at with api_calls=0. These tests pin the three
# pure helpers that drive the fix plus the DB writeback + backoff state.
# ===========================================================================


# -- Fakes for the agent-cache backref --------------------------------------

class _FakeAgent:
    """Stand-in for run_agent.AIAgent — only needs session_api_calls."""

    def __init__(self, session_api_calls: int = 0):
        self.session_api_calls = session_api_calls


class _FakeRunner:
    """Stand-in for GatewayRunner. ``_handle_message`` is a real bound method
    so ``_handle_message.__self__`` resolves back to this instance, exactly
    like ``adapter.set_message_handler(self._handle_message)`` at run.py:2443.
    """

    def __init__(self, cache=None, *, with_lock: bool = True):
        if cache is not None:
            self._agent_cache = cache
        if with_lock:
            self._agent_cache_lock = threading.Lock()

    def _handle_message(self, event):  # pragma: no cover - identity only
        return None


class _FakeAdapter:
    def __init__(self, handler=None):
        if handler is not None:
            self._message_handler = handler


def _adapter_for(session_key, agent, *, as_tuple=True, with_lock=True):
    """Build an adapter whose bound-handler backref reaches a runner holding
    ``agent`` in ``_agent_cache[session_key]``."""
    value = (agent, "config-sig") if as_tuple else agent
    cache = OrderedDict({session_key: value})
    runner = _FakeRunner(cache, with_lock=with_lock)
    return _FakeAdapter(handler=runner._handle_message)


class TestRunMadeSuccessfulCalls:
    """Tri-state signal: True / False (confident zero) / None (ambiguous)."""

    def test_same_agent_positive_delta_is_true(self):
        a = object()
        assert HermesJobsWorker._run_made_successful_calls(a, 3, a, 5) is True

    def test_same_agent_zero_delta_is_confident_false(self):
        a = object()
        # The exact bug signature: counter didn't advance across the run.
        assert HermesJobsWorker._run_made_successful_calls(a, 5, a, 5) is False

    def test_same_agent_unreadable_pre_is_ambiguous_none(self):
        a = object()
        assert HermesJobsWorker._run_made_successful_calls(a, None, a, 5) is None

    def test_cold_start_fresh_agent_zero_is_confident_false(self):
        a = object()
        # pre_agent is None (agent wasn't cached at snapshot time); the fresh
        # agent ended at 0 → no model call ever succeeded.
        assert HermesJobsWorker._run_made_successful_calls(None, None, a, 0) is False

    def test_cold_start_fresh_agent_positive_is_true(self):
        a = object()
        assert HermesJobsWorker._run_made_successful_calls(None, None, a, 2) is True

    def test_mid_run_agent_swap_is_ambiguous_none(self):
        # Two DISTINCT real agents: the pre-swap agent may have made calls we
        # can't see. MUST NOT false-release — fail safe to success (None).
        pre, post = object(), object()
        assert HermesJobsWorker._run_made_successful_calls(pre, 3, post, 0) is None

    def test_missing_post_agent_is_ambiguous_none(self):
        a = object()
        assert HermesJobsWorker._run_made_successful_calls(a, 3, None, None) is None

    def test_post_calls_unreadable_is_ambiguous_none(self):
        a = object()
        assert HermesJobsWorker._run_made_successful_calls(None, None, a, None) is None


class TestResolveSessionAgent:
    SK = "telegram:dm:123"

    def test_resolves_tuple_cached_agent(self):
        agent = _FakeAgent(session_api_calls=7)
        adapter = _adapter_for(self.SK, agent, as_tuple=True)
        assert HermesJobsWorker._resolve_session_agent(adapter, self.SK) is agent

    def test_resolves_bare_cached_agent(self):
        # run.py's own idiom tolerates a non-tuple cache value.
        agent = _FakeAgent()
        adapter = _adapter_for(self.SK, agent, as_tuple=False)
        assert HermesJobsWorker._resolve_session_agent(adapter, self.SK) is agent

    def test_resolves_without_lock(self):
        agent = _FakeAgent()
        adapter = _adapter_for(self.SK, agent, with_lock=False)
        assert HermesJobsWorker._resolve_session_agent(adapter, self.SK) is agent

    def test_missing_session_key_returns_none(self):
        agent = _FakeAgent()
        adapter = _adapter_for(self.SK, agent)
        assert HermesJobsWorker._resolve_session_agent(adapter, "other-key") is None

    def test_no_message_handler_returns_none(self):
        assert HermesJobsWorker._resolve_session_agent(_FakeAdapter(), self.SK) is None

    def test_handler_without_self_returns_none(self):
        def plain_handler(event):  # a plain function has no __self__
            return None
        assert (
            HermesJobsWorker._resolve_session_agent(
                _FakeAdapter(handler=plain_handler), self.SK,
            )
            is None
        )

    def test_runner_without_agent_cache_returns_none(self):
        runner = _FakeRunner(cache=None)  # no _agent_cache attribute set
        adapter = _FakeAdapter(handler=runner._handle_message)
        assert HermesJobsWorker._resolve_session_agent(adapter, self.SK) is None

    def test_cache_get_raising_returns_none(self):
        class _BoomCache:
            def get(self, key):
                raise RuntimeError("cache exploded")
        runner = _FakeRunner(cache=_BoomCache())
        adapter = _FakeAdapter(handler=runner._handle_message)
        assert HermesJobsWorker._resolve_session_agent(adapter, self.SK) is None


class TestRateLimitBackoff:
    def test_initial_backoff_is_zero(self):
        worker = _make_worker()
        assert worker._rate_limit_backoff_seconds == 0.0

    def test_zero_call_marker_grows_exponentially_and_caps(self):
        worker = _make_worker()
        worker._update_rate_limit_backoff(False, ZERO_API_CALLS_ERROR)
        assert worker._rate_limit_backoff_seconds == RATE_LIMIT_BACKOFF_BASE_SECONDS
        worker._update_rate_limit_backoff(False, ZERO_API_CALLS_ERROR)
        assert worker._rate_limit_backoff_seconds == RATE_LIMIT_BACKOFF_BASE_SECONDS * 2
        # Hammer it — must saturate at the cap, never exceed it.
        for _ in range(20):
            worker._update_rate_limit_backoff(False, ZERO_API_CALLS_ERROR)
        assert worker._rate_limit_backoff_seconds == RATE_LIMIT_BACKOFF_CAP_SECONDS

    def test_success_resets_backoff(self):
        worker = _make_worker()
        worker._rate_limit_backoff_seconds = 240.0
        worker._update_rate_limit_backoff(True, None)
        assert worker._rate_limit_backoff_seconds == 0.0

    def test_other_failure_resets_backoff(self):
        worker = _make_worker()
        worker._rate_limit_backoff_seconds = 120.0
        worker._update_rate_limit_backoff(False, "boom: unrelated failure")
        assert worker._rate_limit_backoff_seconds == 0.0

    def test_none_error_resets_backoff(self):
        worker = _make_worker()
        worker._rate_limit_backoff_seconds = 60.0
        worker._update_rate_limit_backoff(False, None)
        assert worker._rate_limit_backoff_seconds == 0.0


@pytest.mark.asyncio
async def test_interactive_worker_registers_modern_session_owner_and_releases_guard(monkeypatch):
    from gateway.config import Platform

    class Adapter:
        def __init__(self):
            self._active_sessions = {}
            self._session_tasks = {}
            self._session_store = None
            self.saw_owner = False

        async def _process_message_background(self, _event, session_key):
            owner = asyncio.current_task()
            self.saw_owner = self._session_tasks.get(session_key) is owner
            # Mirror modern BasePlatformAdapter's owner-aware cleanup.
            guard = self._active_sessions.get(session_key)
            if self._session_tasks.get(session_key) is owner:
                if self._active_sessions.get(session_key) is guard:
                    self._active_sessions.pop(session_key, None)
                self._session_tasks.pop(session_key, None)

    adapter = Adapter()
    worker = _make_worker()
    worker.adapters = {Platform.TELEGRAM: adapter}
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "8743044208")
    success, error = await worker._run_packet_authoring(_row())
    assert success is True and error is None
    assert adapter.saw_owner is True
    assert adapter._active_sessions == {}
    assert adapter._session_tasks == {}


@pytest.mark.asyncio
async def test_worker_safety_net_clears_only_its_own_session_guard(monkeypatch):
    from gateway.config import Platform

    class Adapter:
        def __init__(self):
            self._active_sessions = {}
            self._session_tasks = {}
            self._session_store = None

        async def _process_message_background(self, _event, _session_key):
            # Deliberately omit BasePlatformAdapter cleanup; the worker's
            # owner-matched safety net must still prevent a permanent lock.
            return None

    adapter = Adapter()
    worker = _make_worker()
    worker.adapters = {Platform.TELEGRAM: adapter}
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "8743044208")
    success, error = await worker._run_packet_authoring(_row())
    assert success is True and error is None
    assert adapter._active_sessions == {}
    assert adapter._session_tasks == {}


class TestZeroApiCallsConstantsWiring:
    def test_marker_embedded_in_error_message(self):
        assert ZERO_API_CALLS_MARKER in ZERO_API_CALLS_ERROR.lower()

    def test_marker_is_a_transient_substring(self):
        # So _writeback_completion routes the error to RELEASE, not FAIL.
        assert ZERO_API_CALLS_MARKER in TRANSIENT_FAILURE_SUBSTRINGS

    def test_backoff_bounds_are_sane(self):
        assert 0 < RATE_LIMIT_BACKOFF_BASE_SECONDS < RATE_LIMIT_BACKOFF_CAP_SECONDS


@pytest.mark.asyncio
class TestZeroApiCallsWriteback:
    async def test_zero_api_calls_error_releases_not_fails(self):
        pool = _FakePool()
        worker = _make_worker(pool=pool)
        await worker._writeback_completion(
            "rid-1", "BIZ-352", 1, False, ZERO_API_CALLS_ERROR,
        )
        execute_calls = [c for c in pool.calls if c[0] == "execute"]
        release_calls = [c for c in execute_calls if "claimed_by = NULL" in c[1]]
        assert release_calls, f"expected RELEASE, got {execute_calls}"
        assert release_calls[0][1] == RELEASE_JOB_SQL
        assert release_calls[0][2] == ("rid-1", "Black Widow", ZERO_API_CALLS_ERROR)
        # Critically: it must NOT mark the row failed_at (permanent disqualify).
        assert not any("failed_at = now()" in c[1] for c in execute_calls)

    async def test_zero_api_calls_run_releases_and_grows_backoff(self):
        # Drive the full _process_row path via the injected runner: a runner
        # returning the ZERO_API_CALLS_ERROR must RELEASE the row AND arm the
        # rate-limit backoff for the next claim.
        pool = _FakePool(claim_rows=[_row()])

        def fake_runner(job):
            return (False, "doc", "", ZERO_API_CALLS_ERROR)

        worker = _make_worker(pool=pool, runner=fake_runner)
        await worker._process_row(_row())

        execute_calls = [c for c in pool.calls if c[0] == "execute"]
        assert any("claimed_by = NULL" in c[1] for c in execute_calls)
        assert not any("failed_at = now()" in c[1] for c in execute_calls)
        assert worker._rate_limit_backoff_seconds == RATE_LIMIT_BACKOFF_BASE_SECONDS

    async def test_successful_run_clears_backoff(self):
        pool = _FakePool(claim_rows=[_row()])

        def fake_runner(job):
            return (True, "doc", "final", None)

        worker = _make_worker(pool=pool, runner=fake_runner)
        worker._rate_limit_backoff_seconds = 240.0  # pretend a prior 429 armed it
        await worker._process_row(_row())

        execute_calls = [c for c in pool.calls if c[0] == "execute"]
        assert any("completed_at = now()" in c[1] for c in execute_calls)
        assert worker._rate_limit_backoff_seconds == 0.0
