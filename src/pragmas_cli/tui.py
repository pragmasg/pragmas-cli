"""The interactive terminal session — Textual TUI, the standard way to run
`pragmas`.

Migrated from a plain Rich REPL (`while True: console.input(...)`) to a
real Textual app: persistent sidebar, independently-scrollable chat log,
resize-aware layout. The REPL's actual logic — `/slash` dispatch, the
Ollama tool-calling bridge, free-text routing — didn't move here at all;
it lives in `dispatch.py`, untouched in behavior, and this module is
purely presentation: it captures that logic's output (via the same
`console.capture()` pattern the old REPL already used for tool-calling,
see `dispatch.run_tool_for_agent`) and re-hosts it into Textual widgets.
`local_agent.py` is untouched too — its `console: Any` parameter was
already duck-typed to just `.print(*args, **kwargs)`, which is what makes
`_TextualConsoleAdapter` below possible without changing a line of it.

Two fundamentally different rendering paths, matched to two different
capture techniques:
- `/slash` commands and the two synchronous free-text branches (CSV
  auto-inspect, "no chat here" refusal) are captured *after* they finish —
  `console.capture()` gives back real ANSI-coded output (the console
  genuinely thinks it's a color terminal), decoded via `rich.text.Text.
  from_ansi(...)` back into a styled `Text` object for a `ChatMessage`.
- Chat turns need real incremental streaming, not a batch dump after
  generation finishes — `_TextualConsoleAdapter` stands in for `console`
  and lazily mounts one `ChatMessage`, appending each `.print()` chunk
  (still `[markup]`-style text, parsed by `ChatMessage.append_markup` via
  `Text.from_markup`, not ANSI — no real terminal is ever involved on this
  path) live as it arrives. It also does two things `local_agent.py` itself
  doesn't know about and shouldn't have to: a small/quantized local model
  (confirmed for real against `llama3.2:1b`, not assumed) sometimes dumps
  the raw tool-definition JSON as plain `content` instead of a real
  `tool_calls` entry — a user report caught this, and it's suppressed here
  (never shown verbatim; tool schemas are for the model, not a human) — and
  it flags the app's "tools: active" sidebar state the first time it sees a
  real tool actually run.

`local_agent.run_chat_turn` (and everything it calls, including tool
execution via `dispatch.run_tool_for_agent`) is synchronous and blocking —
run entirely inside a `run_worker(thread=True)` background thread, never on
Textual's own asyncio event loop, or the whole UI would freeze for the
duration of an LLM generation. Every widget mutation from that thread goes
through `App.call_from_thread(...)`, the only thread-safe way to touch
Textual state from outside the main thread.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.theme import Theme
from textual.widgets import Header, Input, Static

from pragmas_cli import __version__, dispatch
from pragmas_cli.config import config_dir
from pragmas_cli.tui_widgets import ChatMessage, CommandHistoryMixin, QuickCommandButton, Sidebar

# Fixed, deliberately not a config option — the 6 commands worth one click.
_QUICK_COMMANDS = ["analyze", "market", "inspect", "templates", "help", "exit"]

# Commands that make sense to run with zero args (prefill *and* submit);
# everything else in _QUICK_COMMANDS just prefills the input so the user
# can add the required file/topic before pressing Enter.
_ZERO_ARG_COMMANDS = {"help", "templates"}

# How each message role's row aligns in the chat log — "chat bubble" look
# (user right, assistant/command/error/warning left, the startup banner
# centered as a notice rather than a conversation turn). Textual aligns the
# *children* of a container, not a widget's own position within one, so
# this is applied to the Horizontal "row" wrapper each ChatMessage is
# mounted inside (see `mount_chat_message`), not the message itself.
_ROW_ALIGN: dict[str, str] = {
    "user": "right",
    "assistant": "left",
    "system": "center",
    "command": "left",
    "error": "left",
    "warning": "left",
}

# The exact literal strings local_agent.py's run_chat_turn prints for its
# role-prefix marker and tool-call transparency line — matched here to
# treat them specially (the prefix is redundant with the bubble's own role
# header and dropped; the tool notice always shows immediately, never
# buffered for JSON-detection like real content is). Coupling to another
# module's literal print() text is fragile in the abstract, but this is the
# same trade-off `_run_tool_for_agent` already accepts for the "no --email"
# style error text, and local_agent.py is intentionally not touched to fix
# a presentation-layer problem.
_ASSISTANT_PREFIX = "[bold green]assistant>[/bold green] "
_TOOL_NOTICE_PREFIX = "[dim]  -> running "
_TOO_MANY_TOOLS_PREFIX = "[yellow](stopped after too many tool calls"

_PRAGMAS_THEME = Theme(
    name="pragmas",
    primary="#7c3aed",
    success="#10b981",
    warning="#f59e0b",
    error="#ef4444",
    foreground="#e2e8f0",
    background="#0f0f1a",
    surface="#1e1e2e",
    panel="#1e1e2e",
    dark=True,
    variables={"text-muted": "#94a3b8"},
)


class _TextualConsoleAdapter:
    """Duck-types `rich.console.Console`'s `.print(*args, **kwargs)` — the
    only method `local_agent.py`'s `run_chat_turn` ever calls on its
    `console` param. Mounts (or reuses a pre-mounted "thinking…" placeholder
    — see `PragmasApp._submit_line`) one `ChatMessage` bubble and streams
    real content into it live. Always invoked from the background dispatch
    worker thread, never the UI thread — every mutation goes through
    `call_from_thread`.

    Content is buffered one "round" at a time (a round = everything printed
    between one `_ASSISTANT_PREFIX` and the next, or the end of the turn)
    rather than streamed character-by-character unconditionally: the first
    non-whitespace character decides whether this round reads as prose
    (streamed live, exactly as before) or as JSON (`{`/`[` — a small model
    dumping tool-definition/tool-call JSON as plain content instead of a
    real `tool_calls` entry, confirmed for real against `llama3.2:1b`, not
    hypothetical) — JSON is held back entirely and replaced with one honest
    line once the round ends, never shown verbatim.
    """

    def __init__(self, app: "PragmasApp", placeholder: ChatMessage | None = None) -> None:
        self._app = app
        self._widget = placeholder
        self._used = False
        self._round_buffer = ""
        self._round_is_json_like: bool | None = None

    @property
    def used(self) -> bool:
        """Whether any real (non-JSON-garbage) content or tool notice was
        ever actually shown — lets the caller know whether a pre-mounted
        "thinking…" placeholder needs cleaning up because the chat branch
        turned out not to be reached at all this turn."""
        return self._used

    def print(self, *args: Any, **kwargs: Any) -> None:
        text = "".join(str(a) for a in args)
        end = kwargs.get("end", "\n")

        if text == _ASSISTANT_PREFIX and end == "":
            # Redundant with the bubble's own role header — dropped, but
            # still marks a round boundary so the previous round's buffered
            # content (if any) gets judged and flushed now.
            self._flush_round()
            return

        if text.startswith(_TOOL_NOTICE_PREFIX) or text.startswith(_TOO_MANY_TOOLS_PREFIX):
            self._flush_round()
            if text.startswith(_TOOL_NOTICE_PREFIX):
                self._app.call_from_thread(self._app.mark_tool_used)
            self._emit(text if end == "" else text + end)
            return

        chunk = text if end == "" else text + end
        self._round_buffer += chunk
        if self._round_is_json_like is None:
            stripped = self._round_buffer.lstrip()
            if stripped:
                self._round_is_json_like = stripped[0] in "{["
        if self._round_is_json_like is False:
            self._emit(chunk)
            self._round_buffer = ""
        # If True (or still undecided — nothing non-whitespace seen yet),
        # hold everything; decided at the next round boundary or `finalize`.

    def finalize(self) -> None:
        """Call once `run_chat_turn` has fully returned — flushes whatever
        the last round left buffered, which nothing else would ever do
        (only a *new* round's prefix or a tool notice triggers a flush
        otherwise, and there is no next round after the last one)."""
        self._flush_round()

    def _flush_round(self) -> None:
        if not self._round_buffer:
            return
        if self._round_is_json_like:
            self._emit(
                "[dim](the model only produced tool-definition JSON, not a usable "
                "reply or a real tool call, this round — try rephrasing, or /model "
                "to try a different model)[/dim]\n"
            )
        self._round_buffer = ""
        self._round_is_json_like = None

    def _emit(self, chunk: str) -> None:
        self._app.call_from_thread(self._append_chunk, chunk)

    def _append_chunk(self, chunk: str) -> None:
        if self._widget is None:
            self._widget = self._app.mount_chat_message("assistant", "")
        if not self._used:
            self._widget.set_content("")  # clear the "thinking…" placeholder text, if any
            self._used = True
        self._widget.append_markup(chunk)
        self._app.scroll_chat_to_end()


class CommandField(CommandHistoryMixin, Input):
    """The bottom prompt: history (↑/↓) + `/command` Tab-completion, on top
    of a stock Textual `Input`. Key handling is intercepted in `on_key`
    (rather than declared via `BINDINGS`) so it reliably wins over
    Textual's own Tab-moves-focus fallback regardless of binding-resolution
    order — verified empirically (see tests/test_tui_app.py), not assumed.
    """

    def __init__(self, **kwargs: Any) -> None:
        Input.__init__(self, **kwargs)
        CommandHistoryMixin.__init__(self)
        self.command_names = sorted(dispatch.COMMANDS.keys())

    async def _on_key(self, event: events.Key) -> None:
        new_value = await self.handle_command_keys(event, self.value)
        if new_value is not None:
            self.value = new_value
            self.cursor_position = len(new_value)
            return
        await super()._on_key(event)


class PragmasApp(App[None]):
    """The Textual app itself. `run_worker`/`call_from_thread` boundary is
    the one thing to keep straight when touching this class: anything that
    calls into `dispatch.py` (which can block on network/subprocess I/O)
    belongs in a background worker; anything that touches a widget belongs
    on the main thread, reached from a worker via `call_from_thread`.
    """

    TITLE = f"pragmas v{__version__}"
    SUB_TITLE = "checking Ollama…"
    ENABLE_COMMAND_PALETTE = False  # no "^p palette" hint — see the custom #status-bar instead

    CSS = """
    Screen { background: $background; }
    #body { height: 1fr; }
    #chat-scroll { width: 1fr; padding: 1 0; }
    #conn-bar { dock: top; height: 1; padding: 0 1; background: $panel; }
    #sidebar-toggle { dock: left; width: 4; display: none; content-align: center middle; }
    #sidebar-toggle.-visible { display: block; }
    #status-bar { dock: bottom; height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    CommandField { dock: bottom; margin: 0 1 1 1; }
    Sidebar .sidebar-heading { color: $text-muted; }
    .msg-row { width: 1fr; height: auto; }
    .msg-row.-right { align: right top; }
    .msg-row.-left { align: left top; }
    .msg-row.-center { align: center top; }
    """

    BINDINGS: ClassVar[list] = [
        ("ctrl+b", "toggle_sidebar", "Sidebar"),
        ("ctrl+h", "show_help", "Help"),
        ("ctrl+q", "quit_app", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        # None = follow the width breakpoints automatically; True/False =
        # user forced it with Ctrl+B or the [=] button, which then wins
        # over the breakpoint until they toggle it again.
        self._sidebar_forced: bool | None = None
        self.ollama_connected: bool = False
        self.active_model: str | None = None
        self.supports_tools: bool = False
        # Whether a tool has actually been *invoked* this session (not just
        # whether the model supports it) — drives the sidebar/status-bar
        # 3-state "unsupported / available / active" indicator. Plain
        # attributes, not `reactive` fields with `watch_*` hooks: a past
        # bug here (found by code-review) came from relying on per-field
        # reactive watchers whose firing order didn't match assignment
        # order — one explicit `_refresh_status()` call after every field
        # is set has no such ordering dependency.
        self.tools_used_this_session: bool = False
        # Real value computed once in on_mount (see its own comment) — this
        # default just means "not checked yet" for anything that somehow
        # reads it before mount (shouldn't happen in normal use).
        self._r_available: bool = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("checking Ollama…", id="conn-bar")
        with Horizontal(id="body"):
            yield Static("[=]", id="sidebar-toggle")
            yield Sidebar(id="sidebar")
            yield VerticalScroll(id="chat-scroll")
        yield CommandField(placeholder="pragmas> ...", id="prompt")
        yield Static("", id="status-bar")

    # ── startup ───────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.register_theme(_PRAGMAS_THEME)
        self.theme = "pragmas"
        # Computed once here, not re-checked (r_available() is a
        # shutil.which PATH scan) on every _refresh_status_bar() call —
        # _refresh_status() runs after *every* turn, not just at startup,
        # so without caching this repeated the exact same filesystem scan
        # _refresh_env_info() already does once, on every single message.
        from pragmas_sdk.analysis.r_runner import r_available

        self._r_available = r_available()
        full = self._should_show_full_banner()
        self.mount_chat_message("system", self._welcome_message(full))
        if full:
            self._record_banner_shown()
        self._populate_quick_commands()
        self._apply_sidebar_state()
        self._refresh_status()
        self.query_one(CommandField).focus()
        self.run_worker(self._detect_ollama_worker, thread=True, group="ollama-detect")

    def _banner_marker_path(self) -> Path:
        return config_dir() / "last_banner"

    def _should_show_full_banner(self) -> bool:
        """The full ASCII banner is real, useful signal exactly once a day
        — after that it's just something to scroll past to find yesterday's
        conversation. Cosmetic only: any failure reading/writing the marker
        file just means "show it again", never blocks startup."""
        try:
            last = self._banner_marker_path().read_text(encoding="utf-8").strip()
        except OSError:
            last = ""
        return last != date.today().isoformat()

    def _record_banner_shown(self) -> None:
        marker = self._banner_marker_path()
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(date.today().isoformat(), encoding="utf-8")
        except OSError:
            pass

    def _welcome_message(self, full: bool) -> str:
        """Deliberately NOT `main._show_welcome()`'s captured output
        anymore — that prints a "Quick start" panel (now redundant with the
        sidebar's quick-command buttons) and an "Environment" panel (now
        redundant with the sidebar's own Environment section); showing both
        again in the chat log was a real reported bug, not a style
        preference. Just the banner art + a one-line pointer at the
        sidebar/`/help`, and only once a day at that."""
        from pragmas_cli.main import BANNER

        if full:
            return (
                f"{BANNER}\n\n"
                "Operational intelligence, from your terminal. Local-first, open source.\n"
                "Quick commands and environment status are in the sidebar — /help for the full list."
            )
        return "pragmas — /help for commands."

    def _populate_quick_commands(self) -> None:
        container = self.query_one("#quick-commands")
        for name in _QUICK_COMMANDS:
            container.mount(QuickCommandButton(name))
        self._refresh_env_info()

    def _refresh_env_info(self) -> None:
        lines = [
            f"Rscript: {'[green]found[/green]' if self._r_available else '[yellow]not found[/yellow]'}",
            f"Version: {__version__}",
            f"Config:  {config_dir()}",
        ]
        self.query_one("#env-info", Static).update("\n".join(lines))

    def _detect_ollama_worker(self) -> None:
        session = dispatch.start_new_session()
        self.call_from_thread(self._apply_session_state, session)

    def _apply_session_state(self, session: dispatch.AgentSession | None) -> None:
        if session is not None:
            self.ollama_connected = True
            self.active_model = session.model
            self.supports_tools = session.supports_tools
        else:
            self.ollama_connected = False
            self.active_model = None
            self.supports_tools = False
        self.tools_used_this_session = False
        self._refresh_status()

    def mark_tool_used(self) -> None:
        if not self.tools_used_this_session:
            self.tools_used_this_session = True
            self._refresh_status()

    def _tools_state(self) -> tuple[str, str]:
        """Returns (label, theme-color-variable) for the 3-state tools
        indicator. No "●"/"○" glyphs (see tui_widgets.py's module
        docstring for why) — a color-coded word carries the same three
        distinct states without an unverified-on-legacy-consoles glyph:
        muted = can't; yellow = can, hasn't yet; green = has, this
        session."""
        if not self.supports_tools:
            return "unsupported", "text-muted"
        if self.tools_used_this_session:
            return "active", "success"
        return "available", "warning"

    def _refresh_status(self) -> None:
        self._refresh_conn_bar()
        self._refresh_status_bar()

    def _refresh_conn_bar(self) -> None:
        bar = self.query_one("#conn-bar", Static)
        session_info = self.query_one("#session-info", Static)
        tools_label, tools_color = self._tools_state()
        if self.ollama_connected and self.active_model:
            bar.update(f"[green]* Ollama connected[/green] — {self.active_model} ({tools_label})")
            session_info.update(
                f"Mode: [green]Local agent[/green]\nModel: {self.active_model}\n"
                f"Tools: [{tools_color}]{tools_label}[/{tools_color}]"
            )
            self.sub_title = "Local agent mode"
        else:
            bar.update("[red]* Ollama offline[/red] — modo programación (/commands only)")
            session_info.update("Mode: [yellow]Local command[/yellow]\n(no chat — /model to check again)")
            self.sub_title = "Local command mode"

    def _refresh_status_bar(self) -> None:
        bar = self.query_one("#status-bar", Static)
        model_part = f"[bold]{self.active_model}[/bold]" if self.active_model else "[dim]no model[/dim]"
        tools_label, tools_color = self._tools_state()
        r_part = "[green]ok[/green]" if self._r_available else "[red]missing[/red]"
        bar.update(
            f" {model_part}  |  Tools: [{tools_color}]{tools_label}[/{tools_color}]  |  "
            f"Rscript: {r_part}   [dim]Ctrl+H Help   Ctrl+Q Quit[/dim]"
        )

    # ── responsive sidebar ───────────────────────────────────────────────

    def on_resize(self, event: events.Resize) -> None:
        self._last_width = event.size.width
        self._apply_sidebar_state()

    def action_toggle_sidebar(self) -> None:
        currently_collapsed = self.query_one(Sidebar).has_class("-collapsed")
        self._sidebar_forced = not currently_collapsed
        self._apply_sidebar_state()

    async def action_show_help(self) -> None:
        await self._submit_line("/help")

    def action_quit_app(self) -> None:
        self.exit()

    @on(events.Click, "#sidebar-toggle")
    def _on_toggle_click(self) -> None:
        self.action_toggle_sidebar()

    def _apply_sidebar_state(self) -> None:
        width = getattr(self, "_last_width", self.size.width or 100)
        if self._sidebar_forced is not None:
            collapsed = self._sidebar_forced
        else:
            collapsed = width < 80
        sidebar = self.query_one(Sidebar)
        toggle = self.query_one("#sidebar-toggle", Static)
        sidebar.set_class(collapsed, "-collapsed")
        # Below 40 cols there's no room for even the toggle affordance —
        # matches the spec's "sidebar solo chat+input" narrowest breakpoint.
        toggle.set_class(collapsed and width >= 40, "-visible")

    # ── quick-command buttons ────────────────────────────────────────────

    async def on_button_pressed(self, event: Any) -> None:
        button = event.button
        name = getattr(button, "name", None)
        if not name or not (button.id or "").startswith("quick-"):
            return
        if name in ("exit", "quit"):
            self.exit()
            return
        field = self.query_one(CommandField)
        if name in _ZERO_ARG_COMMANDS:
            await self._submit_line(f"/{name}")
        else:
            field.value = f"/{name} "
            field.cursor_position = len(field.value)
            field.focus()

    # ── the actual input -> dispatch -> chat pipeline ───────────────────

    @on(Input.Submitted, "#prompt")
    async def _on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value
        field = self.query_one(CommandField)
        field.value = ""
        await self._submit_line(value)

    async def _submit_line(self, value: str) -> None:
        line = value.strip()
        field = self.query_one(CommandField)
        if not line:
            return
        field.remember(line)
        if line in ("/exit", "/quit", "/q"):
            self.exit()
            return
        self.mount_chat_message("user", line)
        self.scroll_chat_to_end()
        field.disabled = True

        placeholder = None
        if not line.startswith("/") and dispatch.get_active_session() is not None:
            # A "thinking…" placeholder only makes sense for the chat path
            # — slash commands and the CSV-auto-inspect/refusal branches
            # are fast, synchronous, local calls with no real wait to mask.
            placeholder = self.mount_chat_message("assistant", "[dim]thinking…[/dim]")
        self.run_worker(lambda: self._process_line(line, placeholder), thread=True, group="dispatch")

    def _process_line(self, line: str, placeholder: ChatMessage | None) -> None:
        adapter_used = False
        out = err = ""
        try:
            if line.startswith("/"):
                out, err = dispatch.dispatch_captured(line[1:])
            else:
                adapter = _TextualConsoleAdapter(self, placeholder=placeholder)
                out, err = dispatch.handle_free_text(line, console_override=adapter)
                adapter.finalize()
                adapter_used = adapter.used
        except Exception as exc:  # noqa: BLE001 — dispatch_captured already catches
            # everything dispatch() itself can raise, but handle_free_text's
            # synchronous branches (CSV auto-inspect, the refusal panel)
            # have no such wrapping of their own — an unusual file (a
            # non-UTF-8 CSV, common from a Windows/Excel export, raises
            # UnicodeDecodeError straight out of main._scan_csv) can escape
            # uncaught. Found by code-review: without this, that exception
            # skipped every cleanup step below it and left the "thinking…"
            # placeholder stuck forever with no explanation. This is the
            # last line of defense so an unexpected error still reaches the
            # user as text.
            err = f"Unexpected error: {exc}"
        finally:
            if placeholder is not None and not adapter_used:
                # Whether the chat branch was never reached this turn (e.g.
                # CSV auto-inspect took over instead) or the block above
                # raised — either way the "thinking…" placeholder must not
                # linger. Moved into `finally` (was only reachable from the
                # try body's normal-completion path before) so it always
                # runs.
                self.call_from_thread(self._remove_row, placeholder)
            if out.strip():
                self.call_from_thread(self._mount_captured, "command", out)
            if err.strip():
                self.call_from_thread(self._mount_captured, "error", err)
            self.call_from_thread(self._finish_processing)

    def _finish_processing(self) -> None:
        field = self.query_one(CommandField)
        field.disabled = False
        field.focus()
        # A chat turn (tool-calling in particular) can change which model is
        # active or drop the session entirely on a connection failure —
        # reflect that in the sidebar/header without waiting for the next
        # /model or a full re-detect. But _apply_session_state() always
        # resets tools_used_this_session (correct for an actual new
        # session/model), and this method runs after *every* turn — calling
        # it unconditionally here was a real bug: it wiped out the "active"
        # flag mark_tool_used() had just set moments earlier in the very
        # same turn, so the indicator could never advance past "available"
        # (caught by hand, running a real tool call through the app, not by
        # any test written in advance). Only call it on an actual change.
        session = dispatch.get_active_session()
        if session is None:
            changed = self.ollama_connected  # was connected before this turn, isn't now
        else:
            changed = (
                not self.ollama_connected
                or session.model != self.active_model
                or session.supports_tools != self.supports_tools
            )
        if changed:
            self._apply_session_state(session)
        else:
            self._refresh_status()

    # ── mounting helpers (main-thread only) ─────────────────────────────

    def _mount_message(self, role: str, content: Any) -> ChatMessage:
        """Shared by `mount_chat_message` and `_mount_captured` — wraps a
        `ChatMessage` in its aligned `Horizontal` row and mounts it. Was
        written out twice (found by code-review); a future change to
        row-wrapping only has one place to make it now."""
        msg = ChatMessage(role, content)
        align = _ROW_ALIGN.get(role, "left")
        row = Horizontal(msg, classes=f"msg-row -{align}")
        self.query_one("#chat-scroll", VerticalScroll).mount(row)
        return msg

    def mount_chat_message(self, role: str, content: str) -> ChatMessage:
        return self._mount_message(role, content)

    def _mount_captured(self, role: str, ansi_text: str) -> None:
        rendered = Text.from_ansi(ansi_text.rstrip("\n"))
        self._mount_message(role, rendered)
        self.scroll_chat_to_end()

    def _remove_row(self, msg: ChatMessage) -> None:
        """Removes a message's whole row wrapper, not just the message
        widget — leaving an empty `Horizontal` behind after a placeholder
        cleanup is a small, pointless artifact otherwise."""
        parent = msg.parent
        if parent is not None:
            parent.remove()
        else:
            msg.remove()

    def scroll_chat_to_end(self) -> None:
        self.query_one("#chat-scroll", VerticalScroll).scroll_end(animate=False)


def maybe_launch_tui() -> None:
    """Entry point for both bare `pragmas` and `pragmas tui`. Falls back to
    the static welcome screen when stdin isn't a real terminal (piped
    input, CI, `pragmas < /dev/null`) — a Textual app can't run without a
    real tty any more than the old REPL loop could hang waiting on input
    that would never come."""
    if not sys.stdin.isatty():
        from pragmas_cli.main import _show_welcome

        _show_welcome()
        return
    PragmasApp().run()
