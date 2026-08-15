"""Custom Textual widgets for the pragmas TUI.

Kept deliberately small: `ChatMessage` (a message bubble — user / assistant
/ system / command / error / warning) and `CommandInput` (a single-line
`Input` with `/command` history and Tab-completion). Everything else in the
UI is composed from Textual's own stock widgets (`Header`, `Footer`,
`Static`, `ListView`, `VerticalScroll`) directly in `tui.py` — there's no
widget literally named "Sidebar" in Textual itself (it's a layout pattern,
not a widget class), so `Sidebar` here is just a plain `Vertical` container
with a fixed structure the app fills in by widget id.
"""
from __future__ import annotations

import os
from typing import Any, ClassVar

from rich.console import Group, RenderableType
from rich.text import Text
from textual import events
from textual.containers import Vertical
from textual.widgets import ListView, Static


class ChatMessage(Static):
    """One message bubble in the chat log."""

    DEFAULT_CSS = """
    ChatMessage {
        margin: 0 1 1 1;
        padding: 0 1;
        border: round $panel-lighten-2;
        background: $panel;
        width: 1fr;
    }
    ChatMessage.-user { border: round $primary; }
    ChatMessage.-assistant { border: round $success; }
    ChatMessage.-system { border: round $accent; }
    ChatMessage.-command { border: round $panel-lighten-3; }
    ChatMessage.-error { border: round $error; }
    ChatMessage.-warning { border: round $warning; }
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
        label = self._ROLE_LABELS.get(self.role, self.role.title())
        header = Text(label, style="bold")
        body: RenderableType = Text.from_markup(content) if isinstance(content, str) else content
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
    """Layout skeleton only — `tui.py`'s app fills `#session-info`,
    `#quick-commands`, and `#env-info` by id after mount. Not a real
    Textual widget class (Textual has none named "Sidebar"), just a
    `Vertical` container docked left via CSS in `tui.py`."""

    DEFAULT_CSS = """
    Sidebar {
        width: 30;
        min-width: 22;
        dock: left;
        background: $panel;
        border-right: solid $primary;
        padding: 1 1;
    }
    Sidebar.-collapsed {
        display: none;
    }
    Sidebar .sidebar-heading {
        text-style: bold;
        margin-top: 1;
    }
    """

    def compose(self):
        yield Static("[bold]Current session[/bold]", classes="sidebar-heading")
        yield Static("Checking Ollama…", id="session-info")
        yield Static("[bold]Quick commands[/bold]", classes="sidebar-heading")
        yield ListView(id="quick-commands")
        yield Static("[bold]Environment[/bold]", classes="sidebar-heading")
        yield Static("", id="env-info")
