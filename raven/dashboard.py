"""Live Control Room — F10.

An all-black terminal dashboard powered by Rich that gives a real-time view of
RAVEN's state.  One panel per concern:

  ┌──────────────────────────────────────────────┐
  │  MATCH  │  ODDS  │  AGENT STATE  │  RISK     │
  │  QUOTES │  EXPOSURE  │  ACTIONS  │  RECEIPTS │
  └──────────────────────────────────────────────┘

The dashboard is *decoupled from the agent loop*: it consumes a shared
``DashboardState`` dataclass that the main agent pushes updates into.  The
render loop runs in its own thread at a configurable refresh rate (default
4 Hz) so it never blocks the agent.

Usage
-----
    state = DashboardState()
    board = ControlRoom(state)
    board.start()          # launches background render thread
    ...                    # agent pushes updates: state.risk_score = 42 …
    board.stop()
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich import box
    _RICH = True
except ImportError:
    _RICH = False


# ---------------------------------------------------------------------------
# Shared state (written by agent, read by renderer)
# ---------------------------------------------------------------------------

@dataclass
class QuoteSnapshot:
    market: str
    bid: float
    ask: float
    max_size: float
    spread_pct: float


@dataclass
class ActionLog:
    ts_label: str          # e.g. "21:14:03.229"
    action: str            # e.g. "CANCEL_AND_HEDGE"
    reason: str
    detail: str = ""


@dataclass
class ReceiptLog:
    ts_label: str
    solana_tx: str         # short hash or "pending"
    sequence: int
    action: str
    verified: bool


@dataclass
class DashboardState:
    """Mutable snapshot of RAVEN's current status.

    The agent sets these fields directly; the renderer reads them without
    locking (slight staleness is acceptable for a 4-Hz display).
    """

    # Match
    fixture_id: str = "—"
    home_team: str  = "Home"
    away_team: str  = "Away"
    match_clock: str = "00:00"
    score_home: int  = 0
    score_away: int  = 0
    latest_event: str = "—"
    txline_seq: int = 0
    txline_verified: bool = False

    # Agent
    agent_state: str = "NORMAL"   # NORMAL / CAUTION / WITHDRAW / HEDGE / RECALIBRATE / REENTER
    risk_score: float = 0.0
    quotes_placed: int = 0
    quotes_cancelled: int = 0

    # Odds
    odds_home: float = 2.00
    odds_draw: float = 3.50
    odds_away: float = 3.00

    # Exposure
    exposure_home: float = 0.0
    exposure_draw: float = 0.0
    exposure_away: float = 0.0

    # Active quotes
    quotes: List[QuoteSnapshot] = field(default_factory=list)

    # Log tails (ring-buffer semantics: keep last N)
    action_log: List[ActionLog] = field(default_factory=list)
    receipt_log: List[ReceiptLog] = field(default_factory=list)
    _max_log: int = 8

    # Feed mode
    feed_mode: str = "live"   # "live" | "replay"
    replay_speed: float = 1.0

    def push_action(self, action: ActionLog) -> None:
        self.action_log.append(action)
        if len(self.action_log) > self._max_log:
            self.action_log = self.action_log[-self._max_log:]

    def push_receipt(self, receipt: ReceiptLog) -> None:
        self.receipt_log.append(receipt)
        if len(self.receipt_log) > self._max_log:
            self.receipt_log = self.receipt_log[-self._max_log:]


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

_STATE_COLORS = {
    "NORMAL":      "bold green",
    "CAUTION":     "bold yellow",
    "WITHDRAW":    "bold red",
    "HEDGE":       "bold magenta",
    "RECALIBRATE": "bold cyan",
    "REENTER":     "bold blue",
}

def _RISK_COLOR(s):
    return "green" if s < 30 else ("yellow" if s < 60 else ("red" if s < 80 else "bold red"))


class ControlRoom:
    """Rich-based terminal dashboard for RAVEN.

    Parameters
    ----------
    state:
        Shared :class:`DashboardState` instance pushed to by the agent.
    refresh_hz:
        Render refresh rate (default 4 per second).
    """

    def __init__(self, state: DashboardState, refresh_hz: float = 4.0) -> None:
        self._state   = state
        self._delay   = 1.0 / max(0.5, refresh_hz)
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if not _RICH:
            print("[RAVEN] Rich not installed — dashboard disabled.")
            return
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    # -- render loop --------------------------------------------------------

    def _run(self) -> None:
        console = Console(force_terminal=True)
        with Live(
            self._build_layout(),
            console=console,
            refresh_per_second=4,
            screen=True,
        ) as live:
            while self._running:
                live.update(self._build_layout())
                time.sleep(self._delay)

    # -- layout builder -----------------------------------------------------

    def _build_layout(self) -> Layout:
        s = self._state

        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        layout["body"].split_row(
            Layout(name="left"),
            Layout(name="right"),
        )
        layout["left"].split_column(
            Layout(name="match",   size=10),
            Layout(name="quotes",  size=12),
            Layout(name="exposure",size=8),
        )
        layout["right"].split_column(
            Layout(name="agent",   size=8),
            Layout(name="actions", size=12),
            Layout(name="receipts",size=10),
        )

        layout["header"].update(self._header_panel(s))
        layout["match"].update(self._match_panel(s))
        layout["quotes"].update(self._quotes_panel(s))
        layout["exposure"].update(self._exposure_panel(s))
        layout["agent"].update(self._agent_panel(s))
        layout["actions"].update(self._actions_panel(s))
        layout["receipts"].update(self._receipts_panel(s))
        layout["footer"].update(self._footer_panel(s))

        return layout

    # -- panels -------------------------------------------------------------

    def _header_panel(self, s: DashboardState) -> Panel:
        mode_str = (
            f"[dim]REPLAY {s.replay_speed:.0f}×[/dim]"
            if s.feed_mode == "replay"
            else "[green]● LIVE[/green]"
        )
        title = Text.assemble(
            ("🐦‍⬛  RAVEN ", "bold white"),
            ("Real-time Autonomous Verifiable Exposure Neutralizer", "dim white"),
            ("   ", ""),
            (mode_str, ""),
        )
        return Panel(title, style="on black", border_style="cyan")

    def _match_panel(self, s: DashboardState) -> Panel:
        t = Table.grid(padding=(0, 2))
        t.add_column(justify="left")
        t.add_column(justify="center")
        t.add_column(justify="right")

        t.add_row(
            f"[bold]{s.home_team}[/bold]",
            f"[bold white]{s.score_home} — {s.score_away}[/bold white]",
            f"[bold]{s.away_team}[/bold]",
        )
        t.add_row(
            f"[dim]H {s.odds_home:.2f}[/dim]",
            f"[cyan]{s.match_clock}[/cyan]",
            f"[dim]A {s.odds_away:.2f}[/dim]",
        )
        t.add_row("", f"[dim]D {s.odds_draw:.2f}[/dim]", "")
        t.add_row(
            "",
            f"[dim]Event: [/dim][yellow]{s.latest_event}[/yellow]",
            "",
        )
        verified = "✓" if s.txline_verified else "⚠"
        color    = "green" if s.txline_verified else "red"
        t.add_row(
            "",
            f"[{color}]{verified} TxLINE seq #{s.txline_seq}[/{color}]",
            "",
        )
        return Panel(t, title="[bold cyan]MATCH[/bold cyan]", border_style="blue", style="on black")

    def _quotes_panel(self, s: DashboardState) -> Panel:
        t = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style="dim white",
            style="on black",
        )
        t.add_column("Market",    style="cyan",  width=14)
        t.add_column("Bid",       justify="right", style="green")
        t.add_column("Ask",       justify="right", style="red")
        t.add_column("Spread",    justify="right", style="yellow")
        t.add_column("Size",      justify="right", style="dim")

        if s.quotes:
            for q in s.quotes:
                t.add_row(
                    q.market,
                    f"{q.bid:.4f}",
                    f"{q.ask:.4f}",
                    f"{q.spread_pct*100:.2f}%",
                    f"{q.max_size:,.0f}",
                )
        else:
            t.add_row("[dim]—[/dim]", "—", "—", "—", "—")

        return Panel(t, title="[bold cyan]ACTIVE QUOTES[/bold cyan]", border_style="blue", style="on black")

    def _exposure_panel(self, s: DashboardState) -> Panel:
        total = abs(s.exposure_home) + abs(s.exposure_draw) + abs(s.exposure_away)
        t = Table.grid(padding=(0, 2))
        t.add_column(width=12)
        t.add_column(justify="right", width=14)

        def _exp_color(v: float) -> str:
            return "green" if v >= 0 else "red"

        t.add_row("Home",  f"[{_exp_color(s.exposure_home)}]{s.exposure_home:+,.0f} USDC[/]")
        t.add_row("Draw",  f"[{_exp_color(s.exposure_draw)}]{s.exposure_draw:+,.0f} USDC[/]")
        t.add_row("Away",  f"[{_exp_color(s.exposure_away)}]{s.exposure_away:+,.0f} USDC[/]")
        t.add_row("[dim]Total[/dim]", f"[dim]{total:,.0f} USDC[/dim]")

        return Panel(t, title="[bold cyan]EXPOSURE[/bold cyan]", border_style="blue", style="on black")

    def _agent_panel(self, s: DashboardState) -> Panel:
        state_color = _STATE_COLORS.get(s.agent_state, "white")
        risk_color  = _RISK_COLOR(s.risk_score)

        t = Table.grid(padding=(0, 2))
        t.add_column(width=16)
        t.add_column(justify="right")

        t.add_row("State:",  f"[{state_color}]{s.agent_state}[/{state_color}]")
        t.add_row("Risk:",   f"[{risk_color}]{s.risk_score:.1f} / 100[/{risk_color}]")
        t.add_row("Placed:", f"[white]{s.quotes_placed}[/white]")
        t.add_row("Cancelled:", f"[red]{s.quotes_cancelled}[/red]")

        return Panel(t, title="[bold cyan]AGENT[/bold cyan]", border_style="blue", style="on black")

    def _actions_panel(self, s: DashboardState) -> Panel:
        t = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style="dim white",
            style="on black",
        )
        t.add_column("Time",   style="dim",    width=13)
        t.add_column("Action", style="yellow", width=18)
        t.add_column("Reason", style="dim white")

        for a in reversed(s.action_log):
            t.add_row(a.ts_label, a.action, a.reason + (f" {a.detail}" if a.detail else ""))

        return Panel(t, title="[bold cyan]ACTION LOG[/bold cyan]", border_style="blue", style="on black")

    def _receipts_panel(self, s: DashboardState) -> Panel:
        t = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style="dim white",
            style="on black",
        )
        t.add_column("Time",   style="dim",  width=13)
        t.add_column("Seq",    style="dim",  width=6, justify="right")
        t.add_column("Action", style="cyan", width=18)
        t.add_column("Solana", style="dim white")

        for r in reversed(s.receipt_log):
            verified_mark = "[green]✓[/green]" if r.verified else "[red]⚠[/red]"
            t.add_row(
                r.ts_label,
                str(r.sequence),
                r.action,
                f"{verified_mark} {r.solana_tx[:14]}…",
            )

        return Panel(t, title="[bold cyan]SOLANA RECEIPTS[/bold cyan]", border_style="blue", style="on black")

    def _footer_panel(self, s: DashboardState) -> Panel:
        txt = Text.assemble(
            ("  raven-v1.1.0", "dim"),
            ("  │  ", "dim"),
            ("TxLINE", "cyan"),
            (" → Solana devnet", "dim"),
            ("  │  ", "dim"),
            ("verify.ts", "yellow"),
            ("  │  ", "dim"),
            ("q to quit", "dim"),
        )
        return Panel(txt, style="on black", border_style="dim cyan")


# ---------------------------------------------------------------------------
# Fallback plain-text renderer (when Rich is absent)
# ---------------------------------------------------------------------------

class PlainControlRoom:
    """Minimal stdout printer for environments without Rich."""

    def __init__(self, state: DashboardState, refresh_hz: float = 1.0) -> None:
        self._state   = state
        self._delay   = 1.0 / max(0.1, refresh_hz)
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _run(self) -> None:
        while self._running:
            s = self._state
            print(
                f"\r[RAVEN] {s.home_team} {s.score_home}-{s.score_away} {s.away_team} "
                f"| {s.match_clock} | state={s.agent_state} risk={s.risk_score:.0f} "
                f"| seq#{s.txline_seq} {'✓' if s.txline_verified else '⚠'}",
                end="",
                flush=True,
            )
            time.sleep(self._delay)
        print()


def make_control_room(state: DashboardState, refresh_hz: float = 4.0) -> "ControlRoom | PlainControlRoom":
    """Factory: return Rich or plain control room depending on availability."""
    if _RICH:
        return ControlRoom(state, refresh_hz)
    return PlainControlRoom(state, refresh_hz)
