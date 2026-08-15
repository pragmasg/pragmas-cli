"""The interactive terminal session — now the standard way to run `pragmas`.

Phase 1a only: every command wired in here is one of the existing
deterministic, local, no-login commands (`analyze`, `validate`, `inspect`,
`templates`, `market`, `doctor`, `login`, `feedback`). There is no agent chat
in this loop — `ask`/`ingest`/`report generate` stay exactly as stubbed as
they were (see `main.py`'s "v0.2 — agent-backed" section): that needs a
backend-side decision (mapping a beta key to a tenant) that hasn't been made
yet, tracked separately. Free text that isn't a `/command` never pretends to
be a chat agent — it says so explicitly (see `_handle_free_text`) rather than
silently doing nothing useful, the same anti-overpromising standard the rest
of this CLI already holds itself to.

Command handlers call straight into the existing Typer command functions in
`main.py` (e.g. `analyze(...)`) — those are plain functions once decorated
(Typer, unlike Click, returns the original function from `@app.command()`),
so calling them directly in-process is safe *as long as every parameter is
passed explicitly*. Their `typer.Argument(...)`/`typer.Option(...)` defaults
are sentinel objects, not usable values, when bypassing Click's own
invocation — and Click-level validation (`exists=True` on a Path argument,
the `prompt=` on `login`'s `--email`, `min=`/`max=` on `market`'s
`--max-results`) doesn't run at all on a direct call, so this module
re-does the equivalent checks (file-exists, missing email, the 1-10 range)
itself before dispatching. This is a real, standing gap in this
architecture, not just a one-time list to complete: **any** Click-level
constraint added to a command wired in here has to be hand-copied into its
`_cmd_*` handler below or it silently stops applying for TUI users only —
`_cmd_market` is the concrete example to copy the pattern from next time.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Callable

import typer
from rich.panel import Panel
from rich.table import Table

try:
    import readline  # noqa: F401 — Unix-only; hooks input() into GNU readline for
    # history/line-editing. No direct use below. Windows' own console already
    # gives history/editing natively, so ImportError here is expected and fine.
except ImportError:
    pass

from pragmas_cli.main import (
    _list_templates_impl,
    _show_welcome,
    analyze,
    console,
    doctor,
    err_console,
    feedback,
    inspect_command,
    login,
    market,
    templates_show,
    validate,
)


class _DispatchError(Exception):
    """A slash command was malformed in some way that's the user's to fix —
    caught in the main loop and shown as a plain usage Panel, never a
    traceback."""


def _tokenize(rest: str) -> list[str]:
    """Split a command line on whitespace, honoring quotes for multi-word
    args (`/market "consumer spending"`) — but NOT `shlex.split`'s default
    POSIX escape handling, which treats backslash as an escape character
    and silently mangles a bare Windows path (`C:\\Users\\x\\a.csv` loses
    its backslashes). Disabling `.escape` is exactly what `posix=False`
    controls too, but that mode also stops stripping the quote characters
    themselves — this keeps quote-stripping and drops only escape handling.
    """
    try:
        lexer = shlex.shlex(rest, posix=True)
        lexer.whitespace_split = True
        lexer.escape = ""
        return list(lexer)
    except ValueError as exc:
        raise _DispatchError(f"Could not parse command: {exc}") from exc


def _extract_all(tokens: list[str], *names: str) -> tuple[list[str], list[str]]:
    """Remove every `--name value` / `--name=value` pair matching any of
    `names` from `tokens`; return the remaining tokens and every value
    found, in order. Shared core for `_extract_option` (first/only) and
    `_extract_repeated` (all of them, for repeatable flags like --param)."""
    remaining: list[str] = []
    values: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        matched = False
        for name in names:
            if tok == name:
                if i + 1 >= len(tokens):
                    raise _DispatchError(f"{name} needs a value")
                values.append(tokens[i + 1])
                i += 2
                matched = True
                break
            if tok.startswith(name + "="):
                values.append(tok[len(name) + 1 :])
                i += 1
                matched = True
                break
        if not matched:
            remaining.append(tok)
            i += 1
    return remaining, values


def _extract_option(tokens: list[str], *names: str) -> tuple[list[str], str | None]:
    """Like `_extract_all`, but for a flag that should appear at most once —
    returns just the last value seen (or None if absent)."""
    remaining, values = _extract_all(tokens, *names)
    return remaining, values[-1] if values else None


def _extract_repeated(tokens: list[str], *names: str) -> tuple[list[str], list[str]]:
    """`_extract_all`, named for the repeatable-flag call sites (`--param`)."""
    return _extract_all(tokens, *names)


def _extract_flag(tokens: list[str], *names: str) -> tuple[list[str], bool]:
    remaining = [t for t in tokens if t not in names]
    return remaining, len(remaining) != len(tokens)


def _require_csv(tokens: list[str], usage: str) -> Path:
    if not tokens:
        raise _DispatchError(usage)
    path = Path(tokens[0])
    if not path.is_file():
        raise _DispatchError(f"File not found: {path}")
    return path


# ── per-command handlers ─────────────────────────────────────────────────


def _cmd_analyze(tokens: list[str]) -> None:
    tokens, template = _extract_option(tokens, "--template")
    tokens, output = _extract_option(tokens, "--output")
    tokens, output_dir = _extract_option(tokens, "--output-dir")
    tokens, raw_params = _extract_repeated(tokens, "--param")
    path = _require_csv(
        tokens,
        "Usage: /analyze <file.csv> --template <name> [--output table|json|csv] "
        "[--output-dir <dir>] [--param key=value ...]",
    )
    if not template:
        raise _DispatchError("Missing --template. Run /templates to see available names.")
    # analyze() itself parses/coerces/warns on `param` (raw "key=value" strings)
    # internally — same as it does for a real CLI invocation. Nothing to
    # duplicate here.
    analyze(
        input_csv=path,
        template=template,
        output=output or "table",
        output_dir=Path(output_dir) if output_dir else None,
        param=raw_params,
    )


def _cmd_validate(tokens: list[str]) -> None:
    tokens, template = _extract_option(tokens, "--template")
    path = _require_csv(tokens, "Usage: /validate <file.csv> --template <name>")
    if not template:
        raise _DispatchError("Missing --template. Run /templates to see available names.")
    validate(input_csv=path, template=template)


def _cmd_inspect(tokens: list[str]) -> None:
    path = _require_csv(tokens, "Usage: /inspect <file.csv>")
    inspect_command(input_csv=path)


def _cmd_templates(tokens: list[str]) -> None:
    if tokens and tokens[0] == "show":
        if len(tokens) < 2:
            raise _DispatchError("Usage: /templates show <name>")
        templates_show(name=tokens[1])
        return
    if tokens:
        raise _DispatchError("Usage: /templates  or  /templates show <name>")
    _list_templates_impl()


def _cmd_market(tokens: list[str]) -> None:
    tokens, max_results_raw = _extract_option(tokens, "--max-results")
    tokens, output = _extract_option(tokens, "--output")
    topic = " ".join(tokens).strip()
    if not topic:
        raise _DispatchError('Usage: /market <topic> [--max-results N] [--output table|json|md]')
    # market()'s real `typer.Option(5, "--max-results", min=1, max=10)` bound
    # is enforced by Click's own parser, which a direct function call never
    # goes through — re-checked by hand here for the same reason /login
    # re-checks "email required" and /analyze re-checks "file exists": any
    # Click-level constraint (min=/max=/callback=) on a command wired into
    # this TUI has to be re-implemented in its handler or it silently stops
    # applying for TUI users.
    max_results = 5
    if max_results_raw is not None:
        try:
            max_results = int(max_results_raw)
        except ValueError:
            raise _DispatchError(f"--max-results must be a whole number, got {max_results_raw!r}") from None
        if not 1 <= max_results <= 10:
            raise _DispatchError(f"--max-results must be between 1 and 10, got {max_results}")
    market(
        topic=topic,
        max_results=max_results,
        output=output or "table",
    )


def _cmd_doctor(tokens: list[str]) -> None:
    _, check_api = _extract_flag(tokens, "--check-api")
    doctor(check_api=check_api)


def _cmd_login(tokens: list[str]) -> None:
    tokens, email = _extract_option(tokens, "--email")
    _, base_url = _extract_option(tokens, "--base-url")
    if not email:
        email = console.input("Email: ").strip()
    if not email:
        raise _DispatchError("An email is required — it's how your free beta key is issued.")
    from pragmas_cli.config import get_base_url

    login(email=email, base_url=base_url or get_base_url())


def _cmd_feedback(tokens: list[str]) -> None:
    _, open_browser = _extract_flag(tokens, "--open")
    feedback(open_browser=open_browser)


def _cmd_help(_tokens: list[str]) -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="cyan", no_wrap=True)
    table.add_column()
    for name, (_, desc) in _COMMANDS.items():
        table.add_row(f"/{name}", desc)
    table.add_row("/exit, /quit", "Leave the TUI")
    console.print(Panel(table, title="Commands", border_style="cyan", expand=False))
    console.print(
        "[dim]Anything not starting with / is treated as free text — today "
        "that only auto-detects a CSV path (runs /inspect on it). Open-ended "
        "chat needs the agent, which isn't wired into this TUI yet — see "
        "/feedback if that's what you're after.[/dim]"
    )


_COMMANDS: dict[str, tuple[Callable[[list[str]], None], str]] = {
    "analyze": (_cmd_analyze, "Run a deterministic template against a local CSV"),
    "validate": (_cmd_validate, "Check a CSV's columns against a template, before running it"),
    "inspect": (_cmd_inspect, "Suggest templates a local CSV might fit"),
    "templates": (_cmd_templates, "List templates, or 'show <name>' for details"),
    "market": (_cmd_market, "Search public info on a topic"),
    "doctor": (_cmd_doctor, "Check your local PRAGMAS environment"),
    "login": (_cmd_login, "Get a free beta key (only needed for future agent commands)"),
    "feedback": (_cmd_feedback, "Tell us what command or feature you want next"),
    "help": (_cmd_help, "Show this list"),
}


def _dispatch(rest: str) -> None:
    tokens = _tokenize(rest)
    if not tokens:
        raise _DispatchError("Empty command. Type /help for the list.")
    name, *args = tokens
    entry = _COMMANDS.get(name)
    if entry is None:
        raise _DispatchError(f"Unknown command: /{name}. Type /help for the list.")
    handler, _ = entry
    handler(args)


_CSV_SUFFIXES = (".csv",)


def _handle_free_text(line: str) -> None:
    """No open-ended chat here — that's the agent, and it isn't wired into
    this TUI yet (see module docstring). The one thing worth doing with
    free text today: if it looks like a path to a CSV that actually exists,
    just run /inspect on it rather than making the user retype it with a
    slash — everything else gets an honest "not a chat agent yet" message
    instead of silently doing nothing."""
    candidate = line.strip().strip("\"'")
    if candidate.lower().endswith(_CSV_SUFFIXES) and Path(candidate).is_file():
        console.print("[dim]That looks like a CSV path — running /inspect on it.[/dim]\n")
        inspect_command(input_csv=Path(candidate))
        console.print(
            "\n[dim]Pick a template above, then run: /analyze <file> --template <name>[/dim]"
        )
        return
    console.print(
        Panel(
            "This CLI doesn't do open-ended chat yet — that's the PRAGMAS agent "
            "(`ask`), and it isn't wired into the TUI here (needs a backend-side "
            "beta-key/tenant decision that hasn't been made — see `pragmas ask "
            "--help`). Locally, no login needed:\n\n"
            "  /inspect <file.csv>   — see what a CSV might fit\n"
            "  /templates            — list what's available\n"
            "  /help                 — full command list",
            title="Not a chat agent (yet)",
            border_style="yellow",
        )
    )


def run_tui(get_input: Callable[[], str] | None = None) -> None:
    """The interactive loop itself. Takes an optional `get_input` so tests
    (and anything else driving this programmatically) can feed it scripted
    lines without needing a real terminal — `maybe_launch_tui()` below is
    the entry point that decides *whether* to call this in the first place;
    this function has no tty opinion of its own."""
    if get_input is None:
        get_input = lambda: console.input("[bold cyan]pragmas>[/bold cyan] ")  # noqa: E731

    _show_welcome()
    console.print("[dim]Interactive mode — type /help for commands, /exit to quit.[/dim]\n")

    while True:
        try:
            line = get_input()
        except (EOFError, StopIteration):
            console.print()
            break
        except KeyboardInterrupt:
            console.print("\n[dim](interrupted — type /exit to quit)[/dim]")
            continue

        line = line.strip()
        if not line:
            continue
        if line in ("/exit", "/quit", "/q"):
            break

        try:
            if line.startswith("/"):
                _dispatch(line[1:])
            else:
                _handle_free_text(line)
        except typer.Exit:
            # The command already printed its own error Panel via
            # _handle_sdk_errors or similar — just don't let it end the
            # session the way it would end a one-shot CLI invocation.
            continue
        except _DispatchError as exc:
            err_console.print(Panel(str(exc), title="Usage", border_style="red"))
        except Exception as exc:  # noqa: BLE001 — one bad command must never kill the session
            err_console.print(Panel(str(exc), title="Unexpected error", border_style="red"))


def maybe_launch_tui() -> None:
    """Entry point for both bare `pragmas` and `pragmas tui`. Falls back to
    the static welcome screen when stdin isn't a real terminal (piped input,
    CI, `pragmas < /dev/null`) — an interactive prompt loop would otherwise
    hang waiting for input that will never come. Call `run_tui()` directly
    to drive the loop itself, bypassing this check — tests do, since
    Click/Typer's own CliRunner never presents a tty either."""
    if not sys.stdin.isatty():
        _show_welcome()
        return
    run_tui()
