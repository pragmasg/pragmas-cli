"""Custom Textual widgets for the pragmas TUI.

`ChatMessage` (a message bubble — user / assistant / system / command /
error / warning) and `CommandHistoryMixin` (Up/Down history + Tab-complete
for the bottom `Input`) and `Sidebar` (session/quick-commands/environment,
one scrollable region for the whole thing — a nested scroll region just
around quick-commands was tried and reverted, see `Sidebar.compose`'s own
comment for why). Everything else in the UI is composed from Textual's own
stock widgets directly in `tui.py`.

No decorative Unicode glyphs anywhere in this file (no avatar icon, no "●"
status dots) — deliberately, on the same reasoning `tui.py`'s own banner fix
documents: a real legacy-Windows console (cp1252) doesn't just mangle an
out-of-repertoire character when Rich's `Console.print` writes it, it
raises `UnicodeEncodeError` and kills the session. Textual renders through
its own driver, not that code path, so it's *probably* fine here — but that
hasn't been verified against a real legacy console the way the crash-prone
"●"/"→" were, so plain text stays the safe default rather than a second
untested assumption stacked on the first.
"""
from __future__ import annotations

import os
from typing import Any, ClassVar

from rich.console import Group, RenderableType
from rich.text import Text
from textual import events
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, Static


class ChatMessage(Static):
    """One message bubble in the chat log. Mounted inside a `Horizontal`
    "row" wrapper (see `tui.py`'s `mount_chat_message`) that aligns it
    left/right/center per role — Textual aligns *children* of a container,
    not a widget's own position within one, so the row wrapper is what
    actually produces the left/right layout, not this class alone.
    """

    DEFAULT_CSS = """
    ChatMessage {
        width: auto;
        max-width: 80%;
        margin: 0 1 1 1;
        padding: 0 1;
        border: round $panel-lighten-2;
        background: $surface;
    }
    ChatMessage.-user { border: round $primary; background: #2d2d3a; }
    ChatMessage.-assistant { border: round $success; background: $surface; }
    ChatMessage.-command { border: round $panel-lighten-3; background: $surface; }
    ChatMessage.-error { border: round $error; background: $surface; }
    ChatMessage.-warning { border: round $warning; background: $surface; }
    /* "system" (the startup banner) reads as a notice, not a chat turn —
    no border/background, just dim centered text (see the row wrapper's
    center alignment in tui.py). */
    ChatMessage.-system { border: none; background: transparent; max-width: 100%; }
    """

    _ROLE_LABELS: ClassVar[dict[str, str]] = {
        "user": "You",
        "assistant": "Assistant",
        "system": "pragmas",
        "command": "Command",
        "error": "Error",
        "warning": "Warning",
    }

    def __init__(self, role: str, content: RenderableType | str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.role = role
        self.add_class(f"-{role}")
        self._content: RenderableType | str = ""
        self.set_content(content)

    def set_content(self, content: RenderableType | str) -> None:
        """Accepts either a plain string (interpreted as Rich console markup
        — `[dim]...[/dim]` etc., same syntax `local_agent.py`'s own
        `console.print` calls already use) or a ready-made Rich renderable
        (e.g. a `rich.text.Text` produced by `Text.from_ansi(...)` from a
        captured real-Console command's output — see `tui.py`'s
        `_mount_captured`)."""
        self._content = content
        if self.role == "system":
            # No "pragmas" label repeated over the banner itself — it reads
            # as the app talking, not a chat participant.
            body: RenderableType = Text.from_markup(content) if isinstance(content, str) else content
            self.update(body)
            return
        label = self._ROLE_LABELS.get(self.role, self.role.title())
        header = Text(label, style="bold")
        body = Text.from_markup(content) if isinstance(content, str) else content
        self.update(Group(header, body))

    def append_markup(self, chunk: str) -> None:
        """For live streaming (chat turns): accumulate raw markup text and
        re-render the whole bubble. Chat turns are short enough that
        re-rendering on every chunk isn't a real perf concern; there's no
        partial-append API on a Rich renderable to exploit here anyway."""
        if not isinstance(self._content, str):
            self._content = ""
        self.set_content(self._content + chunk)


class CommandHistoryMixin:
    """Mixin-style key handling for a `/command`-aware prompt: Up/Down cycle
    through previously submitted lines, Tab completes a `/command` prefix.
    Applied to `textual.widgets.Input` via subclassing in `tui.py` (kept
    here rather than there only because it's widget behavior, not app
    orchestration) — needs `command_names` supplied by the app so this
    module doesn't have to import `dispatch.py` just for a name list.
    """

    command_names: ClassVar[list[str]] = []

    def __init__(self) -> None:
        self._history: list[str] = []
        self._history_index: int | None = None

    def remember(self, value: str) -> None:
        value = value.strip()
        if value and (not self._history or self._history[-1] != value):
            self._history.append(value)
        self._history_index = None

    def _history_prev(self, current_value: str) -> str | None:
        if not self._history:
            return None
        if self._history_index is None:
            self._history_index = len(self._history) - 1
        else:
            self._history_index = max(0, self._history_index - 1)
        return self._history[self._history_index]

    def _history_next(self) -> str | None:
        if self._history_index is None:
            return None
        if self._history_index >= len(self._history) - 1:
            self._history_index = None
            return ""
        self._history_index += 1
        return self._history[self._history_index]

    def _autocomplete(self, current_value: str) -> str | None:
        if not current_value.startswith("/"):
            return None
        prefix = current_value[1:]
        names = sorted(set(self.command_names) | {"exit", "quit"})
        matches = [n for n in names if n.startswith(prefix)]
        if not matches:
            return None
        if len(matches) == 1:
            return f"/{matches[0]} "
        common = os.path.commonprefix(matches)
        if len(common) > len(prefix):
            return f"/{common}"
        return None

    async def handle_command_keys(self, event: events.Key, current_value: str) -> str | None:
        """Returns the new input value if this key was handled (and the
        caller should stop the event from doing anything else, e.g. moving
        focus on Tab), else None. Kept as a plain method returning a value
        — rather than mutating `self.value` directly — so it works whether
        `self` is the real `Input` widget or a lightweight test double."""
        if event.key == "tab":
            new_value = self._autocomplete(current_value)
            if new_value is not None:
                event.prevent_default()
                event.stop()
                return new_value
            event.prevent_default()
            event.stop()
            return current_value
        if event.key == "up":
            new_value = self._history_prev(current_value)
            if new_value is not None:
                event.prevent_default()
                event.stop()
                return new_value
        elif event.key == "down":
            new_value = self._history_next()
            if new_value is not None:
                event.prevent_default()
                event.stop()
                return new_value
        return None


class Sidebar(Vertical):
    """Layout skeleton only — `tui.py`'s app fills `#session-info` and
    `#env-info` by id after mount, and `#quick-commands` with one `Button`
    per quick command. Not a real Textual widget class (Textual has none
    named "Sidebar"), just a `Vertical` container docked left via CSS in
    `tui.py`. Each section lives in its own `VerticalScroll` so a long
    quick-commands list (or a narrow terminal) scrolls independently rather
    than pushing the sections below it off-screen with no way back.
    """

    DEFAULT_CSS = """
    Sidebar {
        width: 30;
        min-width: 22;
        dock: left;
        background: $panel;
        border-right: solid $primary;
    }
    Sidebar.-collapsed {
        display: none;
    }
    Sidebar .sidebar-heading {
        text-style: bold;
        color: $text-muted;
        margin-top: 1;
        padding: 0 1;
    }
    Sidebar #session-info, Sidebar #env-info {
        padding: 0 1;
        height: auto;
    }
    Sidebar #quick-commands {
        height: auto;
        padding: 0 1;
    }
    Sidebar #quick-commands Button {
        width: 1fr;
        min-width: 0;
        margin-bottom: 1;
    }
    """

    def compose(self):
        # One scroll region for the whole sidebar, not a second one nested
        # around just the quick-commands buttons — a nested VerticalScroll
        # here previously had its own `max-height: 10`, which clipped every
        # button past the first ~2 (6 buttons x 4 rows each = 24 rows,
        # needing scroll to reach) in a way `Pilot.click()` (and a real
        # mouse click at that screen position) couldn't reach at all,
        # confirmed by hand — one flat scrollable region has no such
        # clipped-viewport-within-a-viewport problem.
        with VerticalScroll():
            yield Static("[bold]Current session[/bold]", classes="sidebar-heading")
            yield Static("Checking Ollama…", id="session-info")
            yield Static("[bold]Quick commands[/bold]", classes="sidebar-heading")
            yield Vertical(id="quick-commands")
            yield Static("[bold]Environment[/bold]", classes="sidebar-heading")
            yield Static("", id="env-info")


class QuickCommandButton(Button):
    """A single sidebar quick-command button — `tui.py` reads the command
    name back off `.name` (set at construction) when handling `Pressed`,
    the same "don't dig text back out of a rendered label" lesson learned
    the hard way from the old `ListView` approach (`Label` has no public
    `.renderable` in this Textual version)."""

    def __init__(self, command_name: str) -> None:
        super().__init__(f"/{command_name}", name=command_name, id=f"quick-{command_name}")
