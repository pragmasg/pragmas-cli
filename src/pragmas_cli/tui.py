"""The interactive terminal session — now the standard way to run `pragmas`.

Every `/slash` command wired in here is one of the existing deterministic,
local, no-login commands (`analyze`, `validate`, `inspect`, `templates`,
`market`, `doctor`, `login`, `feedback`, `model`). `ask`/`ingest`/`report
generate` (the PRAGMAS *backend*-agent path) stay exactly as stubbed as they
were (see `main.py`'s "v0.2 — agent-backed" section): that needs a
backend-side decision (mapping a beta key to a tenant) that hasn't been made
yet, tracked separately, and still isn't reachable from here.

Free text that isn't a `/command` has two different fates depending on
whether a local Ollama server was detected at startup (see
`_start_agent_session`, `local_agent.py`):
- **Ollama detected, model available** ("local agent mode"): free text goes
  to that model as a real chat turn, with tool-calling access to
  analyze/inspect/validate/templates/market if the model supports it (see
  `local_agent.TOOLS` / `_run_tool_for_agent`). Fully local — no PRAGMAS
  backend, no beta key, no tenant involved; this is NOT the same thing as
  the backend-agent path above.
- **No Ollama detected** ("local command mode" / "modo programación"): free
  text never pretends to be a chat agent — it says so explicitly (see
  `_handle_free_text`) rather than silently doing nothing useful, the same
  anti-overpromising standard the rest of this CLI already holds itself to.
One CSV-path convenience applies in both modes: typing a path to a CSV that
exists just runs `/inspect` on it.

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

import re
import shlex
import sys
from dataclasses import dataclass, field
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

from pragmas_cli import local_agent
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


# ── local agent mode (Ollama) ────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are the PRAGMAS local terminal agent, running fully offline against "
    "a local Ollama model on the user's own machine — no PRAGMAS account, no "
    "cloud, no data leaves this computer. You have tools to inspect and "
    "analyze local CSV files with deterministic PRAGMAS templates, and to "
    "search public market info. Use a tool whenever the user references a "
    "local file or asks for an analysis — never invent numbers yourself. "
    "Keep answers short and direct."
)


@dataclass
class _AgentSession:
    base_url: str
    model: str
    supports_tools: bool
    messages: list[dict] = field(default_factory=lambda: [{"role": "system", "content": _SYSTEM_PROMPT}])


# Module-level, not a run_tui() local: /model needs to mutate it from its own
# `_cmd_model` handler, and the dispatch table only threads `tokens` through
# to handlers, not arbitrary session state. run_tui() itself resets this at
# the start of every call (never carries over between separate sessions, and
# tests that call run_tui() repeatedly always start from a clean slate).
_active_session: "_AgentSession | None" = None


def _start_agent_session() -> "_AgentSession | None":
    models = local_agent.detect_ollama()
    model = local_agent.pick_default_model(models)
    if model is None:
        return None
    return _AgentSession(base_url=local_agent.ollama_base_url(), model=model.name, supports_tools=model.supports_tools)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_MAX_TOOL_RESULT_CHARS = 4000


def _quote_arg(value: str) -> str:
    return f'"{value}"' if " " in value else value


def _build_tool_command(name: str, args: dict) -> str:
    """Maps an Ollama tool_call (see `local_agent.TOOLS`) to the equivalent
    `/slash` command string, so it can run through `_dispatch` and get every
    bit of existing validation/error-handling for free — including the
    /market --max-results bound fixed above. `analyze`/`market_search` force
    `--output json`: a small local model reads structured JSON far more
    reliably than a Rich-rendered ASCII table.

    Raises `_DispatchError` — same class `_dispatch` itself raises, caught
    the same way by `_run_tool_for_agent` below — for a missing/empty
    required argument, checked explicitly here rather than left to build a
    malformed command string. A blank `template` used to fall through
    silently: `f"--template  --output json"` tokenizes with no empty slot in
    between, so `_extract_option` bound `--output` itself as the template
    value and the real `--output` flag vanished — a small model omitting an
    argument is common enough that this needs a real, actionable error back,
    not a confusing failure two layers down.
    """
    if name == "inspect":
        csv_path = args.get("csv_path") or ""
        if not csv_path:
            raise _DispatchError("inspect needs a csv_path argument")
        return f"inspect {_quote_arg(csv_path)}"
    if name == "list_templates":
        shown = args.get("name")
        return f"templates show {shown}" if shown else "templates"
    if name == "validate_csv":
        csv_path = args.get("csv_path") or ""
        template = args.get("template") or ""
        if not csv_path or not template:
            raise _DispatchError("validate_csv needs both csv_path and template arguments")
        return f"validate {_quote_arg(csv_path)} --template {template}"
    if name == "analyze":
        csv_path = args.get("csv_path") or ""
        template = args.get("template") or ""
        if not csv_path or not template:
            raise _DispatchError("analyze needs both csv_path and template arguments")
        cmd = f"analyze {_quote_arg(csv_path)} --template {template} --output json"
        for kv in args.get("params") or []:
            cmd += f" --param {kv}"
        return cmd
    if name == "market_search":
        topic = args.get("topic") or ""
        if not topic:
            raise _DispatchError("market_search needs a topic argument")
        cmd = f"market {topic} --output json"
        if args.get("max_results"):
            cmd += f" --max-results {args['max_results']}"
        return cmd
    raise _DispatchError(f"Unknown tool: {name!r}")


def _run_tool_for_agent(name: str, args: dict) -> str:
    """The `run_tool` callback handed to `local_agent.run_chat_turn`. Runs
    the mapped slash command with both consoles' output redirected into a
    capture buffer (nothing reaches the real terminal here — the model
    narrates the result back to the user instead of the raw table/JSON
    dump; run the /command yourself if you want that verbatim), catching
    the same three cases run_tui()'s own loop does so a bad tool call comes
    back as text the model can react to, not a crash."""
    with console.capture() as cap_out, err_console.capture() as cap_err:
        try:
            command = _build_tool_command(name, args)
            _dispatch(command)
        except typer.Exit:
            pass
        except _DispatchError as exc:
            err_console.print(f"Usage error: {exc}")
        except Exception as exc:  # noqa: BLE001 — feed the model text, don't crash the chat turn
            err_console.print(f"Unexpected error: {exc}")
    text = _ANSI_RE.sub("", cap_out.get() + cap_err.get()).strip()
    if len(text) > _MAX_TOOL_RESULT_CHARS:
        text = text[:_MAX_TOOL_RESULT_CHARS] + "\n… (truncated)"
    return text or "(no output)"


def _run_chat_turn(user_message: str) -> None:
    global _active_session
    session = _active_session
    if session is None:
        return
    session.messages.append({"role": "user", "content": user_message})
    tools = local_agent.TOOLS if session.supports_tools else None
    try:
        local_agent.run_chat_turn(
            base_url=session.base_url,
            model=session.model,
            messages=session.messages,
            tools=tools,
            run_tool=_run_tool_for_agent,
            console=console,
        )
    except Exception as exc:  # noqa: BLE001 — this is the top-level safety net for the
        # whole Ollama connection, deliberately broad: a dropped connection
        # mid-stream can surface as something other than httpx.HTTPError too
        # (e.g. json.JSONDecodeError on a truncated NDJSON line from
        # _stream_chat) — narrower here originally meant that case fell
        # through to run_tui()'s own generic handler instead, which prints
        # an error but leaves _active_session set, so every next free-text
        # turn kept retrying the same broken connection instead of falling
        # back to command-only mode the way a clean connection-refused did.
        err_console.print(
            Panel(
                f"Lost the local agent turn ({exc}). Falling back to local /commands "
                "only for the rest of this session — restart pragmas to try again.",
                title="Ollama unreachable",
                border_style="red",
            )
        )
        _active_session = None


def _cmd_model(tokens: list[str]) -> None:
    global _active_session
    models = local_agent.detect_ollama()
    if not models:
        raise _DispatchError("No Ollama models detected — is Ollama running?")
    if not tokens:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="cyan", no_wrap=True)
        table.add_column()
        for m in models:
            marker = " (active)" if _active_session and m.name == _active_session.model else ""
            table.add_row(m.name + marker, "tools" if m.supports_tools else "chat only")
        console.print(Panel(table, title="Ollama models", border_style="cyan", expand=False))
        console.print("[dim]Switch with: /model <name>[/dim]")
        return
    name = tokens[0]
    match = next((m for m in models if m.name == name), None)
    if match is None:
        raise _DispatchError(f"Unknown model: {name!r}. Run /model to see what's available.")
    switched_model = _active_session is None or _active_session.model != match.name
    if _active_session is None:
        _active_session = _AgentSession(base_url=local_agent.ollama_base_url(), model=match.name, supports_tools=match.supports_tools)
    else:
        _active_session.model = match.name
        _active_session.supports_tools = match.supports_tools
        if switched_model:
            # Fresh history on an actual switch, not just a no-op re-pick of
            # the same model. Real bug this avoids: an in-progress
            # conversation can carry `tool_calls`/`role: "tool"` messages
            # from the old model; sending those to a new model whose turns
            # get `tools=None` (e.g. switching to a chat-only model) is
            # tool-shaped history with no tool definitions behind it —
            # undefined behavior depending on the model/Ollama version, not
            # something to risk.
            _active_session.messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    tools_note = "with tool access" if match.supports_tools else "chat only, no tool access"
    console.print(f"[green]Switched to {match.name}[/green] ({tools_note}).")


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
    if _active_session is not None:
        console.print(
            f"[dim]Anything not starting with / goes to {_active_session.model} as a "
            "chat message — it can call analyze/inspect/templates/market itself if "
            "the model supports tools. /model to see or switch models.[/dim]"
        )
    else:
        console.print(
            "[dim]Anything not starting with / is treated as free text — today "
            "that only auto-detects a CSV path (runs /inspect on it). No local "
            "Ollama detected, so there's no chat here — /model to check again.[/dim]"
        )


_COMMANDS: dict[str, tuple[Callable[[list[str]], None], str]] = {
    "analyze": (_cmd_analyze, "Run a deterministic template against a local CSV"),
    "validate": (_cmd_validate, "Check a CSV's columns against a template, before running it"),
    "inspect": (_cmd_inspect, "Suggest templates a local CSV might fit"),
    "templates": (_cmd_templates, "List templates, or 'show <name>' for details"),
    "market": (_cmd_market, "Search public info on a topic"),
    "doctor": (_cmd_doctor, "Check your local PRAGMAS environment"),
    "login": (_cmd_login, "Get a free beta key (only needed for future agent commands)"),
    "model": (_cmd_model, "Show/switch the local Ollama model used for chat"),
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
    """One convenience applies regardless of mode: if the line looks like a
    path to a CSV that actually exists, just run /inspect on it rather than
    making the user retype it with a slash. Beyond that, this branches on
    whether a local Ollama model was detected at startup (`_active_session`,
    "local agent mode") — if so, free text is a real chat turn (see
    `_run_chat_turn`, `local_agent.py`). If not ("local command mode" / lo
    que el proyecto llama "modo programación"), free text gets an honest
    "no chat here" message instead of silently doing nothing — same
    anti-overpromising standard as before Ollama detection existed."""
    candidate = line.strip().strip("\"'")
    if candidate.lower().endswith(_CSV_SUFFIXES) and Path(candidate).is_file():
        console.print("[dim]That looks like a CSV path — running /inspect on it.[/dim]\n")
        inspect_command(input_csv=Path(candidate))
        if _active_session is not None:
            console.print("\n[dim]Ask me about it, or run /analyze yourself.[/dim]")
        else:
            console.print(
                "\n[dim]Pick a template above, then run: /analyze <file> --template <name>[/dim]"
            )
        return

    if _active_session is not None:
        _run_chat_turn(line)
        return

    console.print(
        Panel(
            "No local Ollama detected, so there's no chat here — this is 'modo "
            "programación': /commands only. (The PRAGMAS backend agent — `ask` — "
            "is separate and still needs a backend-side beta-key/tenant decision "
            "that hasn't been made yet, unrelated to this.) Locally, no login "
            "needed:\n\n"
            "  /inspect <file.csv>   — see what a CSV might fit\n"
            "  /templates            — list what's available\n"
            "  /model                — check for Ollama again\n"
            "  /help                 — full command list",
            title="No chat agent available",
            border_style="yellow",
        )
    )


def run_tui(get_input: Callable[[], str] | None = None) -> None:
    """The interactive loop itself. Takes an optional `get_input` so tests
    (and anything else driving this programmatically) can feed it scripted
    lines without needing a real terminal — `maybe_launch_tui()` below is
    the entry point that decides *whether* to call this in the first place;
    this function has no tty opinion of its own.

    Probes for a local Ollama model on every call (never carried over from
    a previous run_tui() call in the same process — tests call this
    repeatedly and must each start from a clean slate)."""
    global _active_session
    if get_input is None:
        get_input = lambda: console.input("[bold cyan]pragmas>[/bold cyan] ")  # noqa: E731

    _show_welcome()
    _active_session = _start_agent_session()
    if _active_session is not None:
        tool_note = "with tool access" if _active_session.supports_tools else "chat only, no tool access"
        # "*" not "●" — pure 7-bit ASCII on purpose, same reason as BANNER
        # above: a real legacy-Windows console (cp1252) doesn't just mangle
        # U+25CF, it raises UnicodeEncodeError and kills the whole session.
        # Confirmed by hand against a real console, not just imagined —
        # pytest's capsys doesn't reproduce this (it never goes through
        # Rich's legacy_windows_render path), so it's invisible to the test
        # suite; caught this one manually, not from a failing test.
        console.print(
            f"[bold green]*[/bold green] Local agent mode — connected to Ollama, model "
            f"[bold]{_active_session.model}[/bold] ({tool_note}). Type naturally, or "
            "/command for direct control. /model to switch, /help for the list.\n"
        )
    else:
        console.print(
            "[dim]Local command mode (\"modo programación\") — no local Ollama "
            "detected, so this is /commands only, no chat. Type /help for the "
            "list, or start Ollama and run /model to check again.[/dim]\n"
        )

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
