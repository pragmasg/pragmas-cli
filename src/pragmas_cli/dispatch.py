"""Command dispatch + local-agent bridging — UI-agnostic on purpose.

Ported out of the original Rich-REPL `tui.py` unchanged in behavior (this
is a mechanical extraction, not a rewrite of the logic itself) so the new
Textual `tui.py` and the old Rich-REPL model can both drive the exact same
`/slash` dispatch, tool-calling bridge, and free-text routing. Every
function here that produces user-facing output does so through
`main.py`'s real `console`/`err_console` — captured via `.capture()` and
converted back to a styled `rich.text.Text` on the calling side (see
`tui.py`'s `_mount_captured`) rather than printed directly, since nothing
here knows or cares whether a real terminal or a Textual `Static` widget is
on the other end. This is the exact same `console.capture()` pattern this
codebase already used for tool-calling (`run_tool_for_agent`) before this
file existed — just applied uniformly instead of only at that one seam.

Public surface (no leading underscore): `DispatchError`, `AgentSession`,
`SYSTEM_PROMPT`, `COMMANDS`, `get_active_session`, `start_new_session`,
`dispatch`, `dispatch_captured`, `handle_free_text`, `run_chat_turn`,
`run_tool_for_agent`, `build_tool_command`, `cmd_*`. Everything else
(`_tokenize`, `_extract_*`, `_require_csv`, `_quote_arg`, `_active_session`)
is an internal helper, not meant to be imported elsewhere.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import typer
from rich.panel import Panel
from rich.table import Table

from pragmas_cli import local_agent
from pragmas_cli.main import (
    _list_templates_impl,
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


class DispatchError(Exception):
    """A slash command (or a tool_call mapped to one) was malformed in some
    way that's the user's — or the model's — to fix. Caught at every
    dispatch seam and shown as plain usage text, never a traceback."""


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
        raise DispatchError(f"Could not parse command: {exc}") from exc


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
                    raise DispatchError(f"{name} needs a value")
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
        raise DispatchError(usage)
    path = Path(tokens[0])
    if not path.is_file():
        raise DispatchError(f"File not found: {path}")
    return path


# ── local agent mode (Ollama) ────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are the PRAGMAS local terminal agent, running fully offline against "
    "a local Ollama model on the user's own machine — no PRAGMAS account, no "
    "cloud, no data leaves this computer. You have tools to inspect and "
    "analyze local CSV files with deterministic PRAGMAS templates, and to "
    "search public market info. Use a tool whenever the user references a "
    "local file or asks for an analysis — never invent numbers yourself. "
    "Keep answers short and direct."
)


@dataclass
class AgentSession:
    base_url: str
    model: str
    supports_tools: bool
    messages: list[dict] = field(default_factory=lambda: [{"role": "system", "content": SYSTEM_PROMPT}])


# Module-level, not something callers pass around: /model needs to mutate it
# from its own `cmd_model` handler, and the dispatch table only threads
# `tokens` through to handlers, not arbitrary session state. Call
# `start_new_session()` to (re)detect Ollama and reset this — the Textual
# app does that once at startup; tests do it per-test for a clean slate.
_active_session: AgentSession | None = None


def get_active_session() -> AgentSession | None:
    return _active_session


def start_new_session() -> AgentSession | None:
    """Probes for Ollama and replaces the active session (or clears it to
    None if nothing usable was found). Never carries over stale state from
    a previous call — every caller (the Textual app on mount, tests) gets a
    clean detection each time."""
    global _active_session
    models = local_agent.detect_ollama()
    model = local_agent.pick_default_model(models)
    if model is None:
        _active_session = None
    else:
        _active_session = AgentSession(base_url=local_agent.ollama_base_url(), model=model.name, supports_tools=model.supports_tools)
    return _active_session


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_MAX_TOOL_RESULT_CHARS = 4000


def _quote_arg(value: str) -> str:
    return f'"{value}"' if " " in value else value


def build_tool_command(name: str, args: dict) -> str:
    """Maps an Ollama tool_call (see `local_agent.TOOLS`) to the equivalent
    `/slash` command string, so it can run through `dispatch()` and get
    every bit of existing validation/error-handling for free — including
    the /market --max-results bound (see `cmd_market`). `analyze`/
    `market_search` force `--output json`: a small local model reads
    structured JSON far more reliably than a Rich-rendered ASCII table.

    Raises `DispatchError` for a missing/empty required argument, checked
    explicitly here rather than left to build a malformed command string —
    a blank `template` used to tokenize away entirely, letting
    `_extract_option` bind the *next* flag's name as the value instead of
    raising the intended error; a small model omitting an argument is
    common enough that this needs a real, actionable error back.
    """
    if name == "inspect":
        csv_path = args.get("csv_path") or ""
        if not csv_path:
            raise DispatchError("inspect needs a csv_path argument")
        return f"inspect {_quote_arg(csv_path)}"
    if name == "list_templates":
        shown = args.get("name")
        return f"templates show {shown}" if shown else "templates"
    if name == "validate_csv":
        csv_path = args.get("csv_path") or ""
        template = args.get("template") or ""
        if not csv_path or not template:
            raise DispatchError("validate_csv needs both csv_path and template arguments")
        return f"validate {_quote_arg(csv_path)} --template {template}"
    if name == "analyze":
        csv_path = args.get("csv_path") or ""
        template = args.get("template") or ""
        if not csv_path or not template:
            raise DispatchError("analyze needs both csv_path and template arguments")
        cmd = f"analyze {_quote_arg(csv_path)} --template {template} --output json"
        for kv in args.get("params") or []:
            cmd += f" --param {kv}"
        return cmd
    if name == "market_search":
        topic = args.get("topic") or ""
        if not topic:
            raise DispatchError("market_search needs a topic argument")
        cmd = f"market {topic} --output json"
        if args.get("max_results"):
            cmd += f" --max-results {args['max_results']}"
        return cmd
    raise DispatchError(f"Unknown tool: {name!r}")


def run_tool_for_agent(name: str, args: dict) -> str:
    """The `run_tool` callback handed to `local_agent.run_chat_turn`. Runs
    the mapped slash command with both consoles' output redirected into a
    capture buffer (nothing reaches the real terminal/UI here — the model
    narrates the result back to the user instead of the raw table/JSON
    dump; run the /command yourself if you want that verbatim), catching
    the same cases `dispatch_captured` does so a bad tool call comes back
    as text the model can react to, not a crash."""
    with console.capture() as cap_out, err_console.capture() as cap_err:
        try:
            command = build_tool_command(name, args)
            dispatch(command)
        except typer.Exit:
            pass
        except DispatchError as exc:
            err_console.print(f"Usage error: {exc}")
        except Exception as exc:  # noqa: BLE001 — feed the model text, don't crash the chat turn
            err_console.print(f"Unexpected error: {exc}")
    text = _ANSI_RE.sub("", cap_out.get() + cap_err.get()).strip()
    if len(text) > _MAX_TOOL_RESULT_CHARS:
        text = text[:_MAX_TOOL_RESULT_CHARS] + "\n… (truncated)"
    return text or "(no output)"


def run_chat_turn(user_message: str, *, console_override: Any = None) -> None:
    """Drives one chat turn. `console_override` — if given — is used only
    for the live narrative stream (passed straight through to
    `local_agent.run_chat_turn`'s duck-typed `console` param, unmodified in
    that module); the connection-lost error panel below always goes through
    the real `err_console`, captured by the caller the same way any other
    command's output is (a single, complete panel has no need to stream)."""
    global _active_session
    session = _active_session
    if session is None:
        return
    session.messages.append({"role": "user", "content": user_message})
    tools = local_agent.TOOLS if session.supports_tools else None
    active_console = console_override if console_override is not None else console
    try:
        local_agent.run_chat_turn(
            base_url=session.base_url,
            model=session.model,
            messages=session.messages,
            tools=tools,
            run_tool=run_tool_for_agent,
            console=active_console,
        )
    except Exception as exc:  # noqa: BLE001 — top-level safety net for the whole Ollama
        # connection, deliberately broad: a dropped connection mid-stream
        # can surface as something other than httpx.HTTPError too (e.g.
        # json.JSONDecodeError on a truncated NDJSON line) — narrower here
        # once meant that case left _active_session set, so every next
        # free-text turn kept retrying the same broken connection instead
        # of falling back to command-only mode the way a clean
        # connection-refused already did.
        err_console.print(
            Panel(
                f"Lost the local agent turn ({exc}). Falling back to local /commands "
                "only for the rest of this session — restart pragmas to try again.",
                title="Ollama unreachable",
                border_style="red",
            )
        )
        _active_session = None


def cmd_model(tokens: list[str]) -> None:
    global _active_session
    models = local_agent.detect_ollama()
    if not models:
        raise DispatchError("No Ollama models detected — is Ollama running?")
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
        raise DispatchError(f"Unknown model: {name!r}. Run /model to see what's available.")
    switched_model = _active_session is None or _active_session.model != match.name
    if _active_session is None:
        _active_session = AgentSession(base_url=local_agent.ollama_base_url(), model=match.name, supports_tools=match.supports_tools)
    else:
        _active_session.model = match.name
        _active_session.supports_tools = match.supports_tools
        if switched_model:
            # Fresh history on an actual switch, not just a no-op re-pick of
            # the same model — an in-progress conversation can carry
            # tool_calls/role:"tool" messages from the old model; sending
            # those to a new model whose turns get tools=None (e.g.
            # switching to a chat-only model) is tool-shaped history with no
            # tool definitions behind it, undefined behavior depending on
            # the model/Ollama version.
            _active_session.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    tools_note = "with tool access" if match.supports_tools else "chat only, no tool access"
    console.print(f"[green]Switched to {match.name}[/green] ({tools_note}).")


# ── per-command handlers ─────────────────────────────────────────────────


def cmd_analyze(tokens: list[str]) -> None:
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
        raise DispatchError("Missing --template. Run /templates to see available names.")
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


def cmd_validate(tokens: list[str]) -> None:
    tokens, template = _extract_option(tokens, "--template")
    path = _require_csv(tokens, "Usage: /validate <file.csv> --template <name>")
    if not template:
        raise DispatchError("Missing --template. Run /templates to see available names.")
    validate(input_csv=path, template=template)


def cmd_inspect(tokens: list[str]) -> None:
    path = _require_csv(tokens, "Usage: /inspect <file.csv>")
    inspect_command(input_csv=path)


def cmd_templates(tokens: list[str]) -> None:
    if tokens and tokens[0] == "show":
        if len(tokens) < 2:
            raise DispatchError("Usage: /templates show <name>")
        templates_show(name=tokens[1])
        return
    if tokens:
        raise DispatchError("Usage: /templates  or  /templates show <name>")
    _list_templates_impl()


def cmd_market(tokens: list[str]) -> None:
    tokens, max_results_raw = _extract_option(tokens, "--max-results")
    tokens, output = _extract_option(tokens, "--output")
    topic = " ".join(tokens).strip()
    if not topic:
        raise DispatchError('Usage: /market <topic> [--max-results N] [--output table|json|md]')
    # market()'s real `typer.Option(5, "--max-results", min=1, max=10)` bound
    # is enforced by Click's own parser, which a direct function call never
    # goes through — re-checked by hand here for the same reason /login
    # re-checks "email required" and /analyze re-checks "file exists": any
    # Click-level constraint on a command wired into this dispatch table has
    # to be re-implemented in its handler or it silently stops applying.
    max_results = 5
    if max_results_raw is not None:
        try:
            max_results = int(max_results_raw)
        except ValueError:
            raise DispatchError(f"--max-results must be a whole number, got {max_results_raw!r}") from None
        if not 1 <= max_results <= 10:
            raise DispatchError(f"--max-results must be between 1 and 10, got {max_results}")
    market(
        topic=topic,
        max_results=max_results,
        output=output or "table",
    )


def cmd_doctor(tokens: list[str]) -> None:
    _, check_api = _extract_flag(tokens, "--check-api")
    doctor(check_api=check_api)


def cmd_login(tokens: list[str]) -> None:
    tokens, email = _extract_option(tokens, "--email")
    _, base_url = _extract_option(tokens, "--base-url")
    if not email:
        # No blocking console.input() fallback here (there used to be one) —
        # found by code-review, real bug: the only live caller of cmd_login
        # left standing is the Textual TUI's background dispatch worker
        # (dispatch_captured, run on a run_worker(thread=True) thread while
        # Textual owns the real terminal in raw/alt-screen mode). A blocking
        # input() there can never receive a line — the worker hangs forever,
        # _finish_processing never runs, and the whole app is stuck needing
        # a force-quit. Requiring --email explicitly is the honest fix, not
        # a real interactive prompt dialog (that's a bigger feature).
        raise DispatchError("An email is required — run /login --email you@example.com")
    from pragmas_cli.config import get_base_url

    login(email=email, base_url=base_url or get_base_url())


def cmd_feedback(tokens: list[str]) -> None:
    _, open_browser = _extract_flag(tokens, "--open")
    feedback(open_browser=open_browser)


def cmd_help(_tokens: list[str]) -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="cyan", no_wrap=True)
    table.add_column()
    for name, (_, desc) in COMMANDS.items():
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


COMMANDS: dict[str, tuple[Callable[[list[str]], None], str]] = {
    "analyze": (cmd_analyze, "Run a deterministic template against a local CSV"),
    "validate": (cmd_validate, "Check a CSV's columns against a template, before running it"),
    "inspect": (cmd_inspect, "Suggest templates a local CSV might fit"),
    "templates": (cmd_templates, "List templates, or 'show <name>' for details"),
    "market": (cmd_market, "Search public info on a topic"),
    "doctor": (cmd_doctor, "Check your local PRAGMAS environment"),
    "login": (cmd_login, "Get a free beta key (only needed for future agent commands)"),
    "model": (cmd_model, "Show/switch the local Ollama model used for chat"),
    "feedback": (cmd_feedback, "Tell us what command or feature you want next"),
    "help": (cmd_help, "Show this list"),
}


def dispatch(rest: str) -> None:
    tokens = _tokenize(rest)
    if not tokens:
        raise DispatchError("Empty command. Type /help for the list.")
    name, *args = tokens
    entry = COMMANDS.get(name)
    if entry is None:
        raise DispatchError(f"Unknown command: /{name}. Type /help for the list.")
    handler, _ = entry
    handler(args)


def dispatch_captured(rest: str) -> tuple[str, str]:
    """Runs a /slash command (already stripped of its leading /) with both
    consoles captured — same error handling the original Rich-REPL loop
    had inline, just redirected into a buffer instead of a live terminal.
    Never raises. Returns (stdout_text, stderr_text)."""
    with console.capture() as cap_out, err_console.capture() as cap_err:
        try:
            dispatch(rest)
        except typer.Exit:
            pass
        except DispatchError as exc:
            err_console.print(Panel(str(exc), title="Usage", border_style="red"))
        except Exception as exc:  # noqa: BLE001 — one bad command must never kill the session
            err_console.print(Panel(str(exc), title="Unexpected error", border_style="red"))
    return cap_out.get(), cap_err.get()


_CSV_SUFFIXES = (".csv",)


def handle_free_text(line: str, *, console_override: Any = None) -> tuple[str, str]:
    """One convenience applies regardless of mode: if the line looks like a
    path to a CSV that actually exists, just run /inspect on it. Beyond
    that, branches on whether a local Ollama model is active
    (`get_active_session()`, "local agent mode") — chat turns are driven
    live via `run_chat_turn`/`console_override` rather than captured here
    (they need real incremental streaming, not a batch after the fact); the
    other two branches are fully synchronous and captured like any other
    command. Returns (stdout_text, stderr_text) — normally empty for the
    chat branch, since its narrative content was already delivered live via
    `console_override`; a connection-loss failure is the one thing
    `run_chat_turn` still always sends to the real `err_console` instead
    (a single, complete panel, not something that needs to stream) — that
    call is wrapped in `err_console.capture()` here so it comes back in
    `stderr_text` instead of silently going to the real terminal/stderr a
    Textual caller doesn't own. Found by code-review: without this, a
    Textual user watching the mode silently flip to "modo programación"
    with the actual explanation never reaching them."""
    candidate = line.strip().strip("\"'")
    is_csv = candidate.lower().endswith(_CSV_SUFFIXES) and Path(candidate).is_file()

    if is_csv:
        with console.capture() as cap_out:
            console.print("[dim]That looks like a CSV path — running /inspect on it.[/dim]\n")
            inspect_command(input_csv=Path(candidate))
            if _active_session is not None:
                console.print("\n[dim]Ask me about it, or run /analyze yourself.[/dim]")
            else:
                console.print(
                    "\n[dim]Pick a template above, then run: /analyze <file> --template <name>[/dim]"
                )
        return cap_out.get(), ""

    if _active_session is not None:
        with err_console.capture() as cap_err:
            run_chat_turn(line, console_override=console_override)
        return "", cap_err.get()

    with err_console.capture() as cap_err:
        err_console.print(
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
    return "", cap_err.get()
