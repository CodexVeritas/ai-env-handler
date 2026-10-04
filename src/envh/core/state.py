"""In-memory state: pending requests, live sessions, active runs. Pure logic with an injected clock."""

from __future__ import annotations

import asyncio
import secrets as random_secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Literal

DETACHED_GRACE = timedelta(minutes=10)
HISTORY_RETENTION = timedelta(hours=1)
MAX_PENDING_PER_USER = 20
RequestKind = Literal["session", "run", "import", "preset"]
Outcome = Literal["approved", "denied", "withdrawn"]


class StateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Provenance:
    pid: int
    uid: int
    cmdline: str


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    by: str
    at: datetime


@dataclass
class Request:
    id: int
    kind: RequestKind
    mapping: dict[str, str]
    presets: tuple[str, ...]
    requested: timedelta | None
    granted: timedelta | None
    reason: str | None
    command: tuple[str, ...]
    provenance: Provenance
    created_at: datetime
    summary: dict[str, Any]
    decision: asyncio.Future[Decision]
    detached: bool = False
    abandon_at: datetime | None = None
    result: dict[str, Any] = field(default_factory=dict)

    @property
    def decided(self) -> bool:
        return self.decision.done()

    @property
    def pending(self) -> bool:
        return not self.decision.done()


@dataclass
class Session:
    id: str
    mapping: dict[str, str]
    presets: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    reason: str | None
    provenance: Provenance
    ended_at: datetime | None = None

    def live(self, now: datetime) -> bool:
        return self.ended_at is None and now < self.expires_at


@dataclass
class Run:
    id: int
    mapping: dict[str, str]
    session_id: str | None
    reason: str | None
    command: tuple[str, ...]
    provenance: Provenance
    started_at: datetime
    ended_at: datetime | None = None


class StateTable:
    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._next_id = 1
        self.requests: dict[int, Request] = {}
        self.sessions: dict[str, Session] = {}
        self.runs: dict[int, Run] = {}

    def now(self) -> datetime:
        return self._clock()

    def _allocate_id(self) -> int:
        allocated = self._next_id
        self._next_id += 1
        return allocated

    def new_request(
        self,
        kind: RequestKind,
        mapping: dict[str, str],
        provenance: Provenance,
        reason: str | None,
        command: tuple[str, ...] = (),
        presets: tuple[str, ...] = (),
        requested: timedelta | None = None,
        granted: timedelta | None = None,
        summary: dict[str, Any] | None = None,
    ) -> Request:
        if sum(1 for request in self.pending() if request.provenance.uid == provenance.uid) >= MAX_PENDING_PER_USER:
            raise StateError(f"uid {provenance.uid} already has {MAX_PENDING_PER_USER} requests waiting on the console; wait for those to be decided")
        request = Request(
            id=self._allocate_id(),
            kind=kind,
            mapping=dict(mapping),
            presets=tuple(presets),
            requested=requested,
            granted=granted,
            reason=reason,
            command=tuple(command),
            provenance=provenance,
            created_at=self.now(),
            summary=dict(summary or {}),
            decision=asyncio.get_running_loop().create_future(),
        )
        self.requests[request.id] = request
        return request

    def pending(self) -> list[Request]:
        return [request for request in self.requests.values() if request.pending]

    def decide(self, request: Request, approved: bool, by: str) -> Decision:
        if request.decided:
            raise StateError(f"request #{request.id} was already decided")
        decision = Decision(outcome="approved" if approved else "denied", by=by, at=self.now())
        request.decision.set_result(decision)
        return decision

    def withdraw(self, request: Request) -> None:
        if request.pending:
            request.decision.set_result(Decision(outcome="withdrawn", by="client", at=self.now()))

    def mark_detached(self, request: Request) -> None:
        request.detached = True
        request.abandon_at = self.now() + DETACHED_GRACE

    def sweep_abandoned(self) -> list[Request]:
        now = self.now()
        abandoned = [request for request in self.pending() if request.abandon_at is not None and now >= request.abandon_at]
        for request in abandoned:
            self.withdraw(request)
        return abandoned

    def prune_history(self) -> int:
        cutoff = self.now() - HISTORY_RETENTION
        stale_requests = [rid for rid, request in self.requests.items() if request.decided and request.decision.result().at < cutoff]
        stale_sessions = [sid for sid, session in self.sessions.items() if (session.ended_at or session.expires_at) < cutoff]
        stale_runs = [rid for rid, run in self.runs.items() if run.ended_at is not None and run.ended_at < cutoff]
        for rid in stale_requests:
            del self.requests[rid]
        for sid in stale_sessions:
            del self.sessions[sid]
        for rid in stale_runs:
            del self.runs[rid]
        return len(stale_requests) + len(stale_sessions) + len(stale_runs)

    def create_session(self, request: Request) -> Session:
        if request.granted is None:
            raise StateError("session request has no granted duration")
        now = self.now()
        session = Session(
            id=random_secrets.token_hex(8),
            mapping=dict(request.mapping),
            presets=request.presets,
            created_at=now,
            expires_at=now + request.granted,
            reason=request.reason,
            provenance=request.provenance,
        )
        self.sessions[session.id] = session
        return session

    def rename_secret(self, old: str, new: str) -> None:
        """Point live sessions and every request at a secret's new name; the value they grant does not change. Decided
        requests are included because an approved run reads its request's mapping when it starts, which can be later."""
        for holder in [*self.live_sessions(), *self.requests.values()]:
            holder.mapping = {var: new if secret == old else secret for var, secret in holder.mapping.items()}

    def names_in_use(self) -> set[str]:
        """The secret names that live sessions and waiting requests point at."""
        return {secret for holder in [*self.live_sessions(), *self.pending()] for secret in holder.mapping.values()}

    def live_sessions(self) -> list[Session]:
        now = self.now()
        return [session for session in self.sessions.values() if session.live(now)]

    def session(self, session_id: str) -> Session:
        session = self.sessions.get(session_id)
        if session is None:
            raise StateError(f"unknown session {session_id}")
        if session.ended_at is not None:
            raise StateError(f"session {session_id} was ended")
        if not session.live(self.now()):
            raise StateError(f"session {session_id} expired at {session.expires_at:%H:%M:%S}")
        return session

    def end_session(self, session_id: str) -> Session:
        session = self.session(session_id)
        session.ended_at = self.now()
        return session

    def start_run(
        self,
        mapping: dict[str, str],
        session_id: str | None,
        reason: str | None,
        command: tuple[str, ...],
        provenance: Provenance,
    ) -> Run:
        run = Run(
            id=self._allocate_id(),
            mapping=dict(mapping),
            session_id=session_id,
            reason=reason,
            command=tuple(command),
            provenance=provenance,
            started_at=self.now(),
        )
        self.runs[run.id] = run
        return run

    def end_run(self, run_id: int) -> Run:
        run = self.runs[run_id]
        if run.ended_at is None:
            run.ended_at = self.now()
        return run

    def active_runs(self) -> list[Run]:
        return [run for run in self.runs.values() if run.ended_at is None]
