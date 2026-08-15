"""Local-only chat/tool-calling agent over Ollama.

Deliberately has nothing to do with the PRAGMAS backend, the beta key, or a
tenant — this talks straight to a local Ollama server (default
`http://127.0.0.1:11434`) on the user's own machine. That's what makes it
buildable today: the backend-agent path (`ask`/`ingest`/`report generate`,
still stubbed) is blocked on a beta-key-to-tenant mapping that hasn't been
designed yet; this isn't, because there's no tenant here at all.

Detected once at TUI startup (see `tui.py`'s `_start_agent_session`) — if
Ollama isn't reachable or has no usable model, the TUI stays in the plain
`/slash`-command mode ("modo programación") this module has nothing to do
with. Tool-calling specifically only works with a model Ollama itself
reports as `"tools"`-capable (`/api/tags`' `capabilities` list) — a model
without it still gets plain chat, honestly labeled as such in `tui.py`'s
banner, rather than silently sending a `tools=` payload Ollama will just
ignore for that model.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import httpx

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DETECT_TIMEOUT = 1.5
CHAT_TIMEOUT = 120.0
MAX_TOOL_ROUNDS = 6


def ollama_base_url() -> str:
    """`PRAGMAS_OLLAMA_URL` takes precedence (our own override); falls back
    to Ollama's own `OLLAMA_HOST` convention (host:port, no scheme) if set;
    else the Ollama default.

    `OLLAMA_HOST` is Ollama's *server* bind-address variable — `0.0.0.0`
    there means "listen on every interface" (a normal setup for LAN/Docker
    access, confirmed present on a real dev machine this way), not a valid
    address for a *client* to connect to. Naively reusing it verbatim made
    `detect_ollama()` silently return `[]` against a real, running Ollama —
    found by actually running this against that machine, not in a mocked
    test. Rewritten to `127.0.0.1`, which always reaches a same-machine
    server regardless of which interfaces it's bound to.
    """
    override = os.environ.get("PRAGMAS_OLLAMA_URL") or os.environ.get("OLLAMA_HOST")
    if not override:
        return DEFAULT_OLLAMA_URL
    if not override.startswith(("http://", "https://")):
        override = f"http://{override}"
    override = override.replace("://0.0.0.0:", "://127.0.0.1:").replace("://[::]:", "://127.0.0.1:")
    return override.rstrip("/")


@dataclass
class OllamaModel:
    name: str
    supports_tools: bool


def detect_ollama(base_url: str | None = None, timeout: float = DETECT_TIMEOUT) -> list[OllamaModel]:
    """Best-effort probe of `GET /api/tags` — returns `[]` on ANY failure
    (not running, wrong port, malformed response) rather than raising. This
    is a "is it there" check that runs on every TUI startup; it must never
    be the thing that crashes or hangs the CLI.

    Embedding-only models (`capabilities: ["embedding"]`, e.g.
    `nomic-embed-text`) are filtered out — they can't do chat completion at
    all, so listing them as a chat option would just be a confusing dead
    end.
    """
    url = base_url or ollama_base_url()
    try:
        resp = httpx.get(f"{url}/api/tags", timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001 — startup probe, must never raise
        return []

    models: list[OllamaModel] = []
    for m in data.get("models", []) or []:
        caps = m.get("capabilities", []) or []
        if "completion" not in caps:
            continue
        name = m.get("name") or m.get("model")
        if not name:
            continue
        models.append(OllamaModel(name=name, supports_tools="tools" in caps))
    return models


def pick_default_model(models: list[OllamaModel]) -> OllamaModel | None:
    """Prefers a tools-capable model (needed for the analyze/inspect/etc.
    tool-calling loop) — falls back to the first chat-capable model
    otherwise, still usable for plain conversation."""
    if not models:
        return None
    for m in models:
        if m.supports_tools:
            return m
    return models[0]


def _stream_chat(base_url: str, model: str, messages: list[dict], tools: list[dict] | None) -> Iterator[dict]:
    """Yields decoded NDJSON chunks from Ollama's `POST /api/chat`."""
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    if tools:
        payload["tools"] = tools
    with httpx.stream("POST", f"{base_url}/api/chat", json=payload, timeout=CHAT_TIMEOUT) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            yield json.loads(line)


def _parse_tool_call_args(raw: Any) -> dict:
    """Ollama normally hands back `function.arguments` already parsed into
    an object, but re-parse defensively in case a given model/build returns
    it JSON-encoded as a string instead (matches the raw OpenAI wire
    format some Ollama versions mirror more literally)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def run_chat_turn(
    *,
    base_url: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    run_tool: Callable[[str, dict], str],
    console: Any,
) -> None:
    """Drives one user turn to completion, including any tool-call rounds.
    Mutates `messages` in place, appending every assistant/tool message
    produced — the caller is responsible for having already appended the
    user's own message before calling this, so history stays complete
    across turns.

    `run_tool` is injected (rather than imported from `tui.py`) to avoid a
    circular import and to keep this module usable/testable without the
    rest of the CLI's Typer/Click machinery.
    """
    for _round in range(MAX_TOOL_ROUNDS):
        content = ""
        tool_calls: list[dict] = []
        console.print("[bold green]assistant>[/bold green] ", end="")
        for chunk in _stream_chat(base_url, model, messages, tools):
            delta = chunk.get("message") or {}
            piece = delta.get("content") or ""
            if piece:
                content += piece
                console.print(piece, end="")
            tool_calls.extend(delta.get("tool_calls") or [])
            if chunk.get("done"):
                break
        console.print()

        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls:
            return

        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            args = _parse_tool_call_args(fn.get("arguments"))
            # ASCII arrow, not "→" (U+2192) — that one isn't in cp1252's
            # repertoire at all and raises UnicodeEncodeError on a real
            # legacy-Windows console, confirmed by hand (see tui.py's
            # run_tui banner fix for the same bug with "●").
            console.print(f"[dim]  -> running {name}({args})...[/dim]")
            result_text = run_tool(name, args)
            messages.append({"role": "tool", "content": result_text})

    console.print(
        "[yellow](stopped after too many tool calls in a row — ask me to continue "
        "if you still need more)[/yellow]"
    )


# ── tool schemas offered to the model ────────────────────────────────────
# Names deliberately distinct from the CLI's own slash-command names
# (`list_templates` not `templates`, `validate_csv` not `validate`,
# `market_search` not `market`) so they read unambiguously in a tool list
# next to a general-purpose model's own vocabulary. `tui.py`'s
# `_build_tool_command` maps these back to the real slash commands.

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "inspect",
            "description": (
                "Inspect a local CSV file: exact row/column counts, best-effort "
                "per-column type detection, and which PRAGMAS analysis templates "
                "its columns might fit. Use this first when the user mentions a "
                "CSV and you don't already know its columns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "csv_path": {"type": "string", "description": "Path to the local CSV file"},
                },
                "required": ["csv_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_templates",
            "description": (
                "List every available PRAGMAS analysis template with a one-line "
                "description, or show full details (required columns, known "
                "params) for one named template."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "A specific template to show details for, e.g. 'saas_metrics'. Omit to list all.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_csv",
            "description": "Check whether a local CSV has the columns a given template needs, without running it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "csv_path": {"type": "string"},
                    "template": {"type": "string", "description": "Template name, e.g. saas_metrics"},
                },
                "required": ["csv_path", "template"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze",
            "description": (
                "Run a deterministic PRAGMAS analysis template against a local CSV "
                "and get the computed metrics back as JSON. Only call this once "
                "you know which template fits — use inspect or list_templates "
                "first if unsure. Never invent numbers yourself; always get them "
                "from this tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "csv_path": {"type": "string"},
                    "template": {"type": "string"},
                    "params": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional template params as 'key=value' strings, e.g. ['cac=500'].",
                    },
                },
                "required": ["csv_path", "template"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "market_search",
            "description": "Search public/macro information on a topic (news, benchmarks). Never touches the user's own data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "max_results": {"type": "integer", "description": "1-10, default 5"},
                },
                "required": ["topic"],
            },
        },
    },
]
