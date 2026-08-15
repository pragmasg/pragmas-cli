"""Tests for dispatch.py — the UI-agnostic /slash dispatch + local-agent
bridge, ported out of the original Rich-REPL tui.py unchanged in behavior.
Exercised directly (not through any UI layer) — `tests/test_tui_app.py`
covers the Textual presentation layer built on top of this."""
import json

import httpx
import pytest
import respx

from pragmas_cli import dispatch, local_agent

BASE = "https://api.pragmas.io"
OLLAMA = local_agent.DEFAULT_OLLAMA_URL


def _ndjson(*chunks) -> str:
    return "\n".join(json.dumps(c) for c in chunks) + "\n"


# ── flag-parsing helpers (pure functions) ───────────────────────────────


def test_extract_option_space_form():
    remaining, value = dispatch._extract_option(["a.csv", "--template", "foo"], "--template")
    assert remaining == ["a.csv"]
    assert value == "foo"


def test_extract_option_equals_form():
    remaining, value = dispatch._extract_option(["a.csv", "--template=foo"], "--template")
    assert remaining == ["a.csv"]
    assert value == "foo"


def test_extract_option_missing_value_raises():
    with pytest.raises(dispatch.DispatchError):
        dispatch._extract_option(["--template"], "--template")


def test_extract_repeated_collects_every_occurrence():
    remaining, values = dispatch._extract_repeated(
        ["a.csv", "--param", "cac=500", "--param", "currency=EUR"], "--param"
    )
    assert remaining == ["a.csv"]
    assert values == ["cac=500", "currency=EUR"]


def test_extract_flag_present_and_absent():
    remaining, present = dispatch._extract_flag(["--check-api"], "--check-api")
    assert remaining == []
    assert present is True
    remaining, present = dispatch._extract_flag([], "--check-api")
    assert present is False


def test_tokenize_preserves_windows_backslashes():
    """Regression: shlex.split()'s default POSIX escape handling mangles a
    bare Windows path — `.escape = ""` is what fixes it."""
    tokens = dispatch._tokenize(r"analyze C:\Users\me\a.csv --template x")
    assert tokens == ["analyze", r"C:\Users\me\a.csv", "--template", "x"]


# ── dispatch / dispatch_captured ─────────────────────────────────────────


def test_dispatch_captured_runs_analyze(cashflow_csv):
    out, err = dispatch.dispatch_captured(f"analyze {cashflow_csv} --template cash_flow_13w --output json")
    assert err == ""
    assert "cash_flow_13w" in out


def test_dispatch_captured_unknown_command_is_a_usage_panel():
    out, err = dispatch.dispatch_captured("nope")
    assert out == ""
    assert "Unknown command: /nope" in err
    assert "Usage" in err


def test_dispatch_captured_empty_command():
    out, err = dispatch.dispatch_captured("")
    assert "Empty command" in err


def test_dispatch_raises_for_direct_callers():
    with pytest.raises(dispatch.DispatchError):
        dispatch.dispatch("nope")


# ── build_tool_command / run_tool_for_agent ─────────────────────────────


def test_build_tool_command_raises_on_missing_or_empty_required_args():
    with pytest.raises(dispatch.DispatchError):
        dispatch.build_tool_command("analyze", {"csv_path": "a.csv"})  # no template
    with pytest.raises(dispatch.DispatchError):
        dispatch.build_tool_command("analyze", {"csv_path": "a.csv", "template": ""})  # blank
    with pytest.raises(dispatch.DispatchError):
        dispatch.build_tool_command("inspect", {})
    with pytest.raises(dispatch.DispatchError):
        dispatch.build_tool_command("validate_csv", {"csv_path": "a.csv"})
    with pytest.raises(dispatch.DispatchError):
        dispatch.build_tool_command("market_search", {})
    with pytest.raises(dispatch.DispatchError):
        dispatch.build_tool_command("does_not_exist", {})


def test_run_tool_for_agent_reports_missing_template_as_a_clean_usage_error():
    result = dispatch.run_tool_for_agent("analyze", {"csv_path": "a.csv"})
    assert "Usage error" in result
    assert "template" in result


def test_run_tool_for_agent_runs_real_inspect(cashflow_csv):
    result = dispatch.run_tool_for_agent("inspect", {"csv_path": str(cashflow_csv)})
    assert "Dataset" in result
    assert "Rows" in result


# ── individual cmd_* handlers (still print via the real console, capsys-testable) ──


def test_cmd_analyze_missing_file(capsys):
    with pytest.raises(dispatch.DispatchError):
        dispatch.cmd_analyze(["does_not_exist.csv", "--template", "cash_flow_13w"])


def test_cmd_login_without_email_raises_instead_of_blocking():
    """Regression (code-review, most severe finding): cmd_login used to
    fall back to a blocking console.input('Email: ') when --email was
    omitted — harmless in the old Rich REPL (ran on the main thread with a
    real prompt loop) but the only live caller left is the Textual TUI's
    background dispatch worker, which can never feed it a line: the worker
    would hang forever and the whole app would need a force-quit. Must
    raise immediately, never call console.input()."""
    with pytest.raises(dispatch.DispatchError, match="email"):
        dispatch.cmd_login([])


def test_cmd_market(capsys, monkeypatch):
    class _FakeDDGS:
        def text(self, query, max_results=5):
            return [{"title": "Reuters", "href": "https://example.com", "body": "Trending down."}]

    monkeypatch.setattr("ddgs.DDGS", _FakeDDGS)
    dispatch.cmd_market(["LATAM", "rates"])
    out = capsys.readouterr().out
    assert "Trending down" in out


def test_cmd_market_max_results_out_of_range():
    with pytest.raises(dispatch.DispatchError):
        dispatch.cmd_market(["rates", "--max-results", "500"])


def test_cmd_market_max_results_not_a_number():
    with pytest.raises(dispatch.DispatchError):
        dispatch.cmd_market(["rates", "--max-results", "notanumber"])


def test_cmd_templates_show_missing_name():
    with pytest.raises(dispatch.DispatchError):
        dispatch.cmd_templates(["show"])


def test_cmd_help_lists_commands(capsys):
    dispatch.cmd_help([])
    out = capsys.readouterr().out
    assert "/analyze" in out
    assert "/model" in out


# ── free text ─────────────────────────────────────────────────────────────


def test_handle_free_text_existing_csv_autoinspects(cashflow_csv):
    out, err = dispatch.handle_free_text(str(cashflow_csv))
    assert err == ""
    assert "running /inspect on it" in out
    assert "Dataset" in out


def test_handle_free_text_no_session_is_honest_refusal():
    out, err = dispatch.handle_free_text("what's my burn rate")
    assert out == ""
    assert "No chat agent available" in err


@respx.mock
def test_handle_free_text_with_session_routes_to_chat():
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, content=_ndjson({"message": {"content": "Hi there."}, "done": True}))
    )
    dispatch._active_session = dispatch.AgentSession(base_url=OLLAMA, model="llama3.2:1b", supports_tools=False)
    out, err = dispatch.handle_free_text("hello")
    # The chat branch never captures here — its content was delivered live
    # via console_override, which defaults to the real console when None.
    assert (out, err) == ("", "")
    assert dispatch._active_session.messages[-1] == {"role": "assistant", "content": "Hi there."}


# ── run_chat_turn ─────────────────────────────────────────────────────────


@respx.mock
def test_run_chat_turn_plain_reply(capsys):
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, content=_ndjson({"message": {"content": "Hello there"}, "done": True}))
    )
    dispatch._active_session = dispatch.AgentSession(base_url=OLLAMA, model="phi", supports_tools=False)
    dispatch.run_chat_turn("hi")
    assert dispatch._active_session.messages[-1] == {"role": "assistant", "content": "Hello there"}


@respx.mock
def test_run_chat_turn_executes_a_real_tool_call(cashflow_csv):
    route = respx.post(f"{OLLAMA}/api/chat")
    route.side_effect = [
        httpx.Response(
            200,
            content=_ndjson(
                {
                    "message": {
                        "content": "",
                        "tool_calls": [{"function": {"name": "inspect", "arguments": {"csv_path": str(cashflow_csv)}}}],
                    },
                    "done": True,
                }
            ),
        ),
        httpx.Response(200, content=_ndjson({"message": {"content": "Looks like cash flow data."}, "done": True})),
    ]
    dispatch._active_session = dispatch.AgentSession(base_url=OLLAMA, model="llama3.2:1b", supports_tools=True)
    dispatch.run_chat_turn("what's in this file")

    assert route.call_count == 2
    tool_msgs = [m for m in dispatch._active_session.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "Dataset" in tool_msgs[0]["content"]
    assert dispatch._active_session.messages[-1] == {"role": "assistant", "content": "Looks like cash flow data."}


@respx.mock
def test_run_chat_turn_falls_back_to_command_mode_on_malformed_stream(capsys):
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(200, content=b"not valid ndjson\n"))
    dispatch._active_session = dispatch.AgentSession(base_url=OLLAMA, model="llama3.2:1b", supports_tools=True)
    dispatch.run_chat_turn("hello")
    err = capsys.readouterr().err
    assert "Ollama unreachable" in err
    assert dispatch.get_active_session() is None


@respx.mock
def test_handle_free_text_connection_failure_is_returned_not_swallowed():
    """Regression (code-review): run_chat_turn's error panel always goes to
    the real err_console, never console_override — handle_free_text's chat
    branch used to just discard it (returning ("", "") unconditionally), so
    a Textual caller had no way to know *why* the session just died. Now
    captured and returned as stderr_text."""
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(200, content=b"not valid ndjson\n"))
    dispatch._active_session = dispatch.AgentSession(base_url=OLLAMA, model="llama3.2:1b", supports_tools=True)

    out, err = dispatch.handle_free_text("hello")

    assert out == ""
    assert "Ollama unreachable" in err
    assert dispatch.get_active_session() is None


def test_run_chat_turn_noop_without_a_session():
    assert dispatch.get_active_session() is None
    dispatch.run_chat_turn("hello")  # must not raise


# ── /model + session lifecycle ──────────────────────────────────────────


def test_cmd_model_no_ollama_is_a_clean_error():
    with pytest.raises(dispatch.DispatchError):
        dispatch.cmd_model([])


def test_cmd_model_lists_available(capsys, monkeypatch):
    monkeypatch.setattr(
        local_agent,
        "detect_ollama",
        lambda *a, **k: [
            local_agent.OllamaModel("llama3.2:1b", supports_tools=True),
            local_agent.OllamaModel("phi:latest", supports_tools=False),
        ],
    )
    dispatch.cmd_model([])
    out = capsys.readouterr().out
    assert "llama3.2:1b" in out
    assert "phi:latest" in out


def test_cmd_model_switch_resets_conversation_history(monkeypatch):
    monkeypatch.setattr(
        local_agent,
        "detect_ollama",
        lambda *a, **k: [
            local_agent.OllamaModel("llama3.2:1b", supports_tools=True),
            local_agent.OllamaModel("phi:latest", supports_tools=False),
        ],
    )
    dispatch.cmd_model(["llama3.2:1b"])
    dispatch._active_session.messages.append({"role": "tool", "content": "leftover"})
    assert len(dispatch._active_session.messages) > 1

    dispatch.cmd_model(["phi:latest"])

    assert dispatch._active_session.messages == [{"role": "system", "content": dispatch.SYSTEM_PROMPT}]


def test_cmd_model_reselecting_same_model_does_not_reset_history(monkeypatch):
    monkeypatch.setattr(
        local_agent, "detect_ollama", lambda *a, **k: [local_agent.OllamaModel("llama3.2:1b", supports_tools=True)]
    )
    dispatch.cmd_model(["llama3.2:1b"])
    dispatch._active_session.messages.append({"role": "user", "content": "keep me"})
    before = list(dispatch._active_session.messages)

    dispatch.cmd_model(["llama3.2:1b"])

    assert dispatch._active_session.messages == before


def test_cmd_model_unknown_name():
    with pytest.raises(dispatch.DispatchError):
        dispatch.cmd_model(["does-not-exist"])


def test_start_new_session_none_when_no_ollama():
    assert dispatch.start_new_session() is None
    assert dispatch.get_active_session() is None


def test_start_new_session_picks_tools_capable_model(monkeypatch):
    monkeypatch.setattr(
        local_agent,
        "detect_ollama",
        lambda *a, **k: [
            local_agent.OllamaModel("phi", supports_tools=False),
            local_agent.OllamaModel("llama3.2:1b", supports_tools=True),
        ],
    )
    session = dispatch.start_new_session()
    assert session is not None
    assert session.model == "llama3.2:1b"
    assert dispatch.get_active_session() is session
