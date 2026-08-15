"""Tests for local_agent.py — the Ollama detection/chat/tool-call layer.
Nothing here talks to a real Ollama server: `respx` mocks every HTTP call,
same pattern test_main.py already uses for the PRAGMAS backend."""
import json

import httpx
import pytest
import respx

from pragmas_cli import local_agent

OLLAMA = "http://127.0.0.1:11434"

# conftest.py's autouse `isolated_config` monkeypatches local_agent.detect_ollama
# to `lambda *a, **k: []` for every test in the suite (so run_tui() elsewhere
# never probes the real, running Ollama on this dev machine) — that would
# make every test *in this file* about detect_ollama itself pointless, since
# they exist specifically to exercise the real implementation. Captured here,
# before any test (and its fixtures) has run, so it's the real function
# regardless of what the module attribute gets patched to later.
_real_detect_ollama = local_agent.detect_ollama


def _ndjson(*chunks) -> str:
    return "\n".join(json.dumps(c) for c in chunks) + "\n"


class _FakeConsole:
    """A minimal stand-in for rich.console.Console — just enough of
    `.print(*args, end=...)` to capture streamed text for assertions,
    without pulling Rich's own rendering into these tests."""

    def __init__(self):
        self.parts: list[str] = []

    def print(self, *args, **kwargs):
        self.parts.append("".join(str(a) for a in args))
        if kwargs.get("end", "\n") == "\n":
            self.parts.append("\n")

    def text(self) -> str:
        return "".join(self.parts)


# ── detection ────────────────────────────────────────────────────────────


@respx.mock
def test_detect_ollama_filters_embedding_only_and_flags_tools():
    respx.get(f"{OLLAMA}/api/tags").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {"name": "llama3.2:1b", "capabilities": ["completion", "tools"]},
                    {"name": "phi:latest", "capabilities": ["completion"]},
                    {"name": "nomic-embed-text:latest", "capabilities": ["embedding"]},
                ]
            },
        )
    )
    models = _real_detect_ollama(base_url=OLLAMA)
    assert {m.name: m.supports_tools for m in models} == {
        "llama3.2:1b": True,
        "phi:latest": False,
    }


def test_detect_ollama_returns_empty_on_connection_error():
    models = _real_detect_ollama(base_url="http://127.0.0.1:1", timeout=0.3)
    assert models == []


@respx.mock
def test_detect_ollama_returns_empty_on_malformed_response():
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, content=b"not json"))
    assert _real_detect_ollama(base_url=OLLAMA) == []


def test_pick_default_model_prefers_tools_capable():
    models = [
        local_agent.OllamaModel("phi", supports_tools=False),
        local_agent.OllamaModel("llama3.2:1b", supports_tools=True),
    ]
    assert local_agent.pick_default_model(models).name == "llama3.2:1b"


def test_pick_default_model_falls_back_to_first_when_none_support_tools():
    models = [local_agent.OllamaModel("phi", supports_tools=False), local_agent.OllamaModel("qwen", supports_tools=False)]
    assert local_agent.pick_default_model(models).name == "phi"


def test_pick_default_model_none_when_empty():
    assert local_agent.pick_default_model([]) is None


def test_ollama_base_url_defaults(monkeypatch):
    monkeypatch.delenv("PRAGMAS_OLLAMA_URL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert local_agent.ollama_base_url() == local_agent.DEFAULT_OLLAMA_URL


def test_ollama_base_url_honors_ollama_host_without_scheme(monkeypatch):
    monkeypatch.delenv("PRAGMAS_OLLAMA_URL", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "192.168.1.50:9999")
    assert local_agent.ollama_base_url() == "http://192.168.1.50:9999"


def test_ollama_base_url_rewrites_bind_all_host_to_localhost(monkeypatch):
    """Regression: found on a real dev machine with OLLAMA_HOST=0.0.0.0:11434
    set (a normal LAN-access config) — detect_ollama() silently returned []
    because 0.0.0.0 isn't a valid *client* target, only a *server* bind
    address. See ollama_base_url()'s own docstring for the full story."""
    monkeypatch.delenv("PRAGMAS_OLLAMA_URL", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:9999")
    assert local_agent.ollama_base_url() == "http://127.0.0.1:9999"


def test_ollama_base_url_pragmas_override_takes_precedence(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:9999")
    monkeypatch.setenv("PRAGMAS_OLLAMA_URL", "http://example.com:1234")
    assert local_agent.ollama_base_url() == "http://example.com:1234"


# ── tool_call argument parsing ───────────────────────────────────────────


def test_parse_tool_call_args_accepts_dict_json_string_or_garbage():
    assert local_agent._parse_tool_call_args({"a": 1}) == {"a": 1}
    assert local_agent._parse_tool_call_args('{"a": 1}') == {"a": 1}
    assert local_agent._parse_tool_call_args("not json") == {}
    assert local_agent._parse_tool_call_args(None) == {}


def test_tools_schema_has_the_expected_names():
    names = {t["function"]["name"] for t in local_agent.TOOLS}
    assert names == {"inspect", "list_templates", "validate_csv", "analyze", "market_search"}


# ── run_chat_turn ─────────────────────────────────────────────────────────


@respx.mock
def test_run_chat_turn_plain_reply_no_tool_calls():
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(
            200,
            content=_ndjson(
                {"message": {"content": "Hello"}, "done": False},
                {"message": {"content": " there"}, "done": True},
            ),
        )
    )
    messages = [{"role": "user", "content": "hi"}]
    console = _FakeConsole()

    local_agent.run_chat_turn(
        base_url=OLLAMA,
        model="phi",
        messages=messages,
        tools=None,
        run_tool=lambda name, args: "unused",
        console=console,
    )

    assert "Hello there" in console.text()
    assert messages[-1] == {"role": "assistant", "content": "Hello there"}


@respx.mock
def test_run_chat_turn_executes_a_tool_call_then_replies():
    route = respx.post(f"{OLLAMA}/api/chat")
    route.side_effect = [
        httpx.Response(
            200,
            content=_ndjson(
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {"function": {"name": "inspect", "arguments": {"csv_path": "a.csv"}}}
                        ],
                    },
                    "done": True,
                }
            ),
        ),
        httpx.Response(200, content=_ndjson({"message": {"content": "Done."}, "done": True})),
    ]

    calls = []

    def fake_run_tool(name, args):
        calls.append((name, args))
        return "tool result text"

    messages = [{"role": "user", "content": "inspect a.csv"}]
    console = _FakeConsole()

    local_agent.run_chat_turn(
        base_url=OLLAMA,
        model="llama3.2:1b",
        messages=messages,
        tools=local_agent.TOOLS,
        run_tool=fake_run_tool,
        console=console,
    )

    assert calls == [("inspect", {"csv_path": "a.csv"})]
    assert route.call_count == 2
    assert any(m.get("role") == "tool" and m.get("content") == "tool result text" for m in messages)
    assert messages[-1] == {"role": "assistant", "content": "Done."}
    assert "Done." in console.text()


@respx.mock
def test_run_chat_turn_stops_after_too_many_tool_rounds(monkeypatch):
    monkeypatch.setattr(local_agent, "MAX_TOOL_ROUNDS", 2)
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(
            200,
            content=_ndjson(
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {"function": {"name": "inspect", "arguments": {"csv_path": "a.csv"}}}
                        ],
                    },
                    "done": True,
                }
            ),
        )
    )
    messages = [{"role": "user", "content": "loop forever"}]
    console = _FakeConsole()

    local_agent.run_chat_turn(
        base_url=OLLAMA,
        model="llama3.2:1b",
        messages=messages,
        tools=local_agent.TOOLS,
        run_tool=lambda name, args: "x",
        console=console,
    )

    assert "too many tool calls" in console.text()
