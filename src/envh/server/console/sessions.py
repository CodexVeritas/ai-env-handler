"""The Sessions screen: live sessions, which can be ended early, and the commands running with keys right now."""

from __future__ import annotations

from typing import TYPE_CHECKING

from envh.server.console.dialogs import Choice, View
from envh.server.console.summaries import login_name, plural
from envh.server.console.tui import BOLD, CYAN, DIM, POINTER, Key, Line, Selection, Span, styled, window

if TYPE_CHECKING:
    from envh.server.console.app import ConsoleApp


def minutes_left(seconds: float) -> str:
    minutes = max(int(seconds // 60), 0)
    return f"{minutes // 60}h {minutes % 60:02}m" if minutes >= 60 else f"{minutes}m"


class SessionsView(View):
    def __init__(self, app: ConsoleApp) -> None:
        super().__init__(app)
        self.selection = Selection()

    def title(self) -> str:
        return "Sessions"

    def hints(self) -> str:
        return "↑↓ move · Enter end the session · Esc back"

    def handle(self, key: Key) -> None:
        sessions = self.app.broker.state.live_sessions()
        if self.selection.move(key, len(sessions)):
            return
        if key == "enter" and sessions:
            session = sessions[min(self.selection.index, len(sessions) - 1)]
            lines = [styled("Commands it already started keep running. New runs in it are refused.")]
            options = [("Keep it", lambda: None), ("End it now", lambda: self._end(session.id))]
            self.app.open(Choice(self.app, f"End session {session.id[:8]}?", options, lines=lines))
        else:
            super().handle(key)

    def _end(self, session_id: str) -> None:
        if self.app.attempt(lambda: self.app.broker.end_session(session_id, by="console")):
            self.app.tell(f"Ended session {session_id[:8]}.", "ok")

    def body(self, columns: int, rows: int) -> list[Line]:
        state = self.app.broker.state
        now = state.now()
        sessions = state.live_sessions()
        runs = state.active_runs()
        lines: list[Line] = [styled(f"  Live sessions · {len(sessions)}", BOLD)]
        focus = 0
        if not sessions:
            lines.append(styled("    None. A session opens when you approve a session request.", DIM))
        for index, session in enumerate(sessions):
            chosen = index == self.selection.index
            if chosen:
                focus = len(lines)
            label = ", ".join(session.presets) or "ad hoc"
            lines.append([
                Span(f"  {POINTER} " if chosen else "    ", CYAN),
                Span(f"{session.id[:8]}  ", BOLD if chosen else ""),
                Span(f"{login_name(session.provenance.uid):<10} {label:<20} "),
                Span(f"ends {session.expires_at:%H:%M} (in {minutes_left((session.expires_at - now).total_seconds())})  ", DIM),
                Span(", ".join(sorted(session.mapping)), DIM),
            ])
            if session.reason:
                lines.append([Span("      "), Span(f'"{session.reason}"', DIM)])
        lines += [[], styled(f"  Running now · {plural(len(runs), 'command')}", BOLD)]
        if not runs:
            lines.append(styled("    None.", DIM))
        for run in runs:
            where = f"session {run.session_id[:8]}" if run.session_id else "approved alone"
            lines.append([
                Span(f"    #{run.id:<5}"),
                Span(f"{login_name(run.provenance.uid):<10} "),
                Span(" ".join(run.command) or "(no command line)"),
                Span(f"  {where} · since {run.started_at:%H:%M} · {', '.join(sorted(run.mapping))}", DIM),
            ])
        return window(lines, focus, rows)
