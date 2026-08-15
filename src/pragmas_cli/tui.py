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
  path) live as it arrives.

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
from typing import Any, ClassVar

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static

from pragmas_cli import __version__, dispatch
from pragmas_cli.config import config_dir
from pragmas_cli.tui_widgets import ChatMessage, CommandHistoryMixin, Sidebar

# Fixed, deliberately not a config option — the 6 commands worth one click.
_QUICK_COMMANDS = ["analyze", "market", "inspect", "templates", "help", "exit"]

# Commands that make sense to run with zero args (prefill *and* submit);
# everything else in _QUICK_COMMANDS just prefills the input so the user
# can add the required file/topic before pressing Enter.
_ZERO_ARG_COMMANDS = {"help", "templates"}


def _show_welcome_text() -> str:
    """Captures `main._show_welcome()`'s real Rich output (banner,
    Quick-start/Environment panels) as ANSI text — reused verbatim, not
    reimplemented, same capture-and-rehost pattern as everything else in
    this module."""
    from pragmas_cli.main import _show_welcome

    with dispatch.console.capture() as cap:
        _show_welcome()
    return cap.get()


class _TextualConsoleAdapter:
    """Duck-types `rich.console.Console`'s `.print(*args, **kwargs)` — the
    only method `local_agent.py`'s `run_chat_turn` ever calls on its
    `console` param. Lazily mounts one `ChatMessage` bubble on first use
    (never created at all if the chat branch isn't actually taken — see
    `dispatch.handle_free_text`) and streams every subsequent `.print()`
    into it. Always invoked from the background dispatch worker thread,
    never the UI thread — every mutation goes through `call_from_thread`.
    """

    def __init__(self, app: "PragmasApp") -> None:
        self._app = app
        self._widget: ChatMessage | None = None

    def print(self, *args: Any, **kwargs: Any) -> None:
        text = "".join(str(a) for a in args)
        end = kwargs.get("end", "\n")
        chunk = text if end == "" else text + end
        self._app.call_from_thread(self._append_chunk, chunk)

    def _append_chunk(self, chunk: str) -> None:
        if self._widget is None:
            self._widget = self._app.mount_chat_message("assistant", "")
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

    CSS = """
    Screen { background: $surface; }
    #body { height: 1fr; }
    #chat-scroll { width: 1fr; padding: 1 0; }
    #conn-bar { dock: top; height: 1; padding: 0 1; background: $panel; }
    #sidebar-toggle { dock: left; width: 4; display: none; content-align: center middle; }
    #sidebar-toggle.-visible { display: block; }
    CommandField { dock: bottom; margin: 0 1; }
    Sidebar .sidebar-heading { color: $text-muted; }
    """

    BINDINGS: ClassVar[list] = [
        ("ctrl+b", "toggle_sidebar", "Sidebar"),
    ]

    ollama_connected: reactive[bool] = reactive(False)
    active_model: reactive[str | None] = reactive(None)
    supports_tools: reactive[bool] = reactive(False)

    def __init__(self) -> None:
        super().__init__()
        # None = follow the width breakpoints automatically; True/False =
        # user forced it with Ctrl+B or the ☰-equivalent button, which then
        # wins over the breakpoint until they toggle it again.
        self._sidebar_forced: bool | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("checking Ollama…", id="conn-bar")
        with Horizontal(id="body"):
            yield Static("[=]", id="sidebar-toggle")
            yield Sidebar(id="sidebar")
            yield VerticalScroll(id="chat-scroll")
        yield CommandField(placeholder="pragmas> ...", id="prompt")
        yield Footer()

    # ── startup ───────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._mount_captured("system", _show_welcome_text())
        self._populate_quick_commands()
        self._apply_sidebar_state()
        self.query_one(CommandField).focus()
        self.run_worker(self._detect_ollama_worker, thread=True, group="ollama-detect")

    def _populate_quick_commands(self) -> None:
        quick = self.query_one("#quick-commands", ListView)
        for name in _QUICK_COMMANDS:
            quick.append(ListItem(Label(f"/{name}"), id=f"quick-{name}"))
        # ListView doesn't highlight anything until the user has pressed a
        # navigation key at least once (confirmed via a real headless run:
        # focus() + Enter with no prior up/down did nothing at all) —
        # pre-selecting the first item means Tab-into-sidebar-then-Enter
        # works immediately, not just after an extra keypress nobody's told
        # to make.
        quick.index = 0
        self._refresh_env_info()

    def _refresh_env_info(self) -> None:
        from pragmas_sdk.analysis.r_runner import r_available

        r_ok = r_available()
        lines = [
            f"Rscript: {'[green]found[/green]' if r_ok else '[yellow]not found[/yellow]'}",
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
        # Explicit, single call rather than per-field `watch_*` reactive
        # hooks — found by code-review: with three separate watchers, the
        # bar/sidebar refresh could fire using an already-stale
        # `supports_tools` (whichever field's watcher happened to run
        # before the others were all assigned), and `supports_tools` itself
        # had no watcher at all, so its own assignment never triggered a
        # refresh — startup with a tools-capable model permanently showed
        # "chat only" regardless of the real capability. One refresh, after
        # every field is set, has no such ordering dependency.
        self._refresh_conn_bar()

    def _refresh_conn_bar(self) -> None:
        bar = self.query_one("#conn-bar", Static)
        session_info = self.query_one("#session-info", Static)
        if self.ollama_connected and self.active_model:
            tool_note = "tools" if self.supports_tools else "chat only"
            bar.update(f"[green]* Ollama connected[/green] — {self.active_model} ({tool_note})")
            session_info.update(
                f"Mode: [green]Local agent[/green]\nModel: {self.active_model}\nTools: "
                f"{'[green]yes[/green]' if self.supports_tools else '[yellow]no[/yellow]'}"
            )
            self.sub_title = "Local agent mode"
        else:
            bar.update("[red]* Ollama offline[/red] — modo programación (/commands only)")
            session_info.update("Mode: [yellow]Local command[/yellow]\n(no chat — /model to check again)")
            self.sub_title = "Local command mode"

    # ── responsive sidebar ───────────────────────────────────────────────

    def on_resize(self, event: events.Resize) -> None:
        self._last_width = event.size.width
        self._apply_sidebar_state()

    def action_toggle_sidebar(self) -> None:
        currently_collapsed = self.query_one(Sidebar).has_class("-collapsed")
        self._sidebar_forced = not currently_collapsed
        self._apply_sidebar_state()

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

    # ── quick-command clicks ─────────────────────────────────────────────

    @on(ListView.Selected, "#quick-commands")
    async def _on_quick_command(self, event: ListView.Selected) -> None:
        # Read the command name off the ListItem's own id (set in
        # _populate_quick_commands as f"quick-{name}") rather than digging
        # the text back out of its Label child — Label has no public
        # `.renderable` in this Textual version (8.x), confirmed by an
        # AttributeError from a real headless run, not assumed.
        item_id = event.item.id or ""
        name = item_id.removeprefix("quick-")
        field = self.query_one(CommandField)
        if name in ("exit", "quit"):
            self.exit()
            return
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
        self.run_worker(lambda: self._process_line(line), thread=True, group="dispatch")

    def _process_line(self, line: str) -> None:
        try:
            if line.startswith("/"):
                out, err = dispatch.dispatch_captured(line[1:])
            else:
                adapter = _TextualConsoleAdapter(self)
                out, err = dispatch.handle_free_text(line, console_override=adapter)
            if out.strip():
                self.call_from_thread(self._mount_captured, "command", out)
            if err.strip():
                self.call_from_thread(self._mount_captured, "error", err)
        finally:
            self.call_from_thread(self._finish_processing)

    def _finish_processing(self) -> None:
        field = self.query_one(CommandField)
        field.disabled = False
        field.focus()
        # A chat turn (tool-calling in particular) can change which model is
        # active or drop the session entirely on a connection failure —
        # reflect that in the sidebar/header without waiting for the next
        # /model or a full re-detect.
        session = dispatch.get_active_session()
        self._apply_session_state(session)

    # ── mounting helpers (main-thread only) ─────────────────────────────

    def mount_chat_message(self, role: str, content: str) -> ChatMessage:
        msg = ChatMessage(role, content)
        self.query_one("#chat-scroll", VerticalScroll).mount(msg)
        return msg

    def _mount_captured(self, role: str, ansi_text: str) -> None:
        rendered = Text.from_ansi(ansi_text.rstrip("\n"))
        msg = ChatMessage(role, rendered)
        self.query_one("#chat-scroll", VerticalScroll).mount(msg)
        self.scroll_chat_to_end()

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
