"""Tests for the interactive session (tui.py).

`run_tui(get_input=...)` is exercised directly with a scripted iterator of
lines rather than through a real terminal or CliRunner (CliRunner's stdin is
never a tty either, so it can only ever reach the non-interactive fallback —
see test_main.py's `test_tui_non_tty_falls_back_to_welcome`). That's the
whole reason `run_tui` takes an injectable `get_input` in the first place.

`/login`'s "no --email given" branch calls `console.input()` directly (the
real blocking prompt, same as a human typing in a terminal) rather than
going through the scripted `get_input` — intentionally not covered here,
same as `run_tui`'s own default prompt isn't; both are the identical
one-line `console.input(...)` pattern, already exercised for real by anyone
who's actually used this interactively.
"""
import json

import httpx
import pytest
import respx

from pragmas_cli import local_agent
from pragmas_cli.tui import (
    _DispatchError,
    _extract_flag,
    _extract_option,
    _extract_repeated,
    run_tui,
)

BASE = "https://api.pragmas.io"


def _ndjson(*chunks) -> str:
    return "\n".join(json.dumps(c) for c in chunks) + "\n"


def _run(lines):
    run_tui(get_input=iter(lines).__next__)


# ── the loop itself ──────────────────────────────────────────────────────


def test_help_lists_commands(capsys):
    _run(["/help", "/exit"])
    out = capsys.readouterr().out
    assert "/analyze" in out
    assert "/templates" in out
    assert "/exit, /quit" in out


@pytest.mark.parametrize("exit_word", ["/exit", "/quit", "/q"])
def test_exit_words_stop_the_loop(exit_word):
    # No assertion needed beyond "this returns" — a real bug here hangs the
    # test until pytest's own timeout, not silently.
    _run([exit_word])


def test_eof_stops_the_loop_cleanly():
    _run([])  # get_input() raises StopIteration on the very first call


def test_blank_lines_are_ignored(capsys):
    _run(["", "   ", "/exit"])
    assert capsys.readouterr().err == ""


def test_unknown_command_shows_usage_and_keeps_going(capsys):
    _run(["/nope", "/help", "/exit"])  # /help after the bad one proves the loop kept going
    result = capsys.readouterr()
    assert "Unknown command: /nope" in result.err
    assert "/analyze" in result.out


def test_keyboard_interrupt_does_not_exit(capsys):
    state = {"n": 0}

    def get_input():
        state["n"] += 1
        if state["n"] == 1:
            raise KeyboardInterrupt
        return "/exit"

    run_tui(get_input=get_input)
    out = capsys.readouterr().out
    assert "interrupted" in out


# ── free text ─────────────────────────────────────────────────────────────


def test_free_text_existing_csv_path_autoinspects(capsys, cashflow_csv):
    _run([str(cashflow_csv), "/exit"])
    out = capsys.readouterr().out
    assert "running /inspect on it" in out
    assert "Dataset" in out


def test_free_text_non_csv_is_honest_about_no_chat_agent(capsys):
    """No Ollama (stubbed to [] by the isolated_config fixture) — "modo
    programación", free text gets an honest refusal, not a fake chat reply."""
    _run(["what's my burn rate this month", "/exit"])
    out = capsys.readouterr().out
    assert "No chat agent available" in out


# ── slash commands wired to existing local functionality ───────────────────


def test_slash_analyze_runs(capsys, cashflow_csv):
    _run([f"/analyze {cashflow_csv} --template cash_flow_13w --output json", "/exit"])
    out = capsys.readouterr().out
    assert "cash_flow_13w" in out


def test_slash_analyze_missing_file(capsys):
    _run(["/analyze does_not_exist.csv --template cash_flow_13w", "/exit"])
    err = capsys.readouterr().err
    assert "File not found" in err


def test_slash_analyze_missing_template(capsys, cashflow_csv):
    _run([f"/analyze {cashflow_csv}", "/exit"])
    err = capsys.readouterr().err
    assert "Missing --template" in err


def test_slash_validate(capsys, cashflow_csv):
    _run([f"/validate {cashflow_csv} --template cash_flow_13w", "/exit"])
    out = capsys.readouterr().out
    assert "required columns" in out.lower()


def test_slash_inspect(capsys, cashflow_csv):
    _run([f"/inspect {cashflow_csv}", "/exit"])
    out = capsys.readouterr().out
    assert "Dataset" in out
    assert "Potential templates" in out


def test_slash_templates_list(capsys):
    _run(["/templates", "/exit"])
    out = capsys.readouterr().out
    assert "cash_flow_13w" in out


def test_slash_templates_show(capsys):
    _run(["/templates show cash_flow_13w", "/exit"])
    out = capsys.readouterr().out
    assert "cash_flow_13w" in out


def test_slash_templates_show_missing_name(capsys):
    _run(["/templates show", "/exit"])
    err = capsys.readouterr().err
    assert "Usage: /templates show" in err


def test_slash_doctor(capsys):
    _run(["/doctor", "/exit"])
    out = capsys.readouterr().out
    assert "pragmas-sdk" in out


def test_slash_market(capsys, monkeypatch):
    class _FakeDDGS:
        def text(self, query, max_results=5):
            return [{"title": "Reuters", "href": "https://example.com", "body": "Trending down."}]

    monkeypatch.setattr("ddgs.DDGS", _FakeDDGS)
    _run(["/market LATAM rates", "/exit"])
    out = capsys.readouterr().out
    assert "Trending down" in out


def _unreachable_ddgs():
    raise AssertionError("DDGS() should never be constructed — bad --max-results must be rejected first")


def test_slash_market_max_results_out_of_range_is_a_clean_error(capsys, monkeypatch):
    """market()'s real --max-results has a Click-enforced min=1/max=10 that a
    direct function call bypasses entirely — this is /market's own
    hand-copied re-check of that bound, not the SDK/backend doing anything."""
    monkeypatch.setattr("ddgs.DDGS", _unreachable_ddgs)
    _run(["/market rates --max-results 500", "/exit"])
    err = capsys.readouterr().err
    assert "--max-results must be between 1 and 10" in err


def test_slash_market_max_results_not_a_number_is_a_clean_error(capsys, monkeypatch):
    monkeypatch.setattr("ddgs.DDGS", _unreachable_ddgs)
    _run(["/market rates --max-results notanumber", "/exit"])
    err = capsys.readouterr().err
    assert "--max-results must be a whole number" in err


def test_slash_feedback(capsys):
    _run(["/feedback", "/exit"])
    out = capsys.readouterr().out
    assert "github.com/pragmasg/pragmas-cli/issues" in out


@respx.mock
def test_slash_login_with_explicit_email_saves_key(capsys, isolated_config):
    respx.post(f"{BASE}/auth/beta-key").mock(
        return_value=httpx.Response(
            201,
            json={"beta_key": "pk_beta_xyz", "email": "dev@example.com", "created_at": "2026-08-01T00:00:00Z"},
        )
    )
    _run(["/login --email dev@example.com", "/exit"])
    out = capsys.readouterr().out
    assert "dev@example.com" in out


# ── flag-parsing helpers (pure functions, worth their own coverage) ────────


def test_extract_option_space_form():
    remaining, value = _extract_option(["a.csv", "--template", "foo"], "--template")
    assert remaining == ["a.csv"]
    assert value == "foo"


def test_extract_option_equals_form():
    remaining, value = _extract_option(["a.csv", "--template=foo"], "--template")
    assert remaining == ["a.csv"]
    assert value == "foo"


def test_extract_option_missing_value_raises():
    with pytest.raises(_DispatchError):
        _extract_option(["--template"], "--template")


def test_extract_repeated_collects_every_occurrence():
    remaining, values = _extract_repeated(
        ["a.csv", "--param", "cac=500", "--param", "currency=EUR"], "--param"
    )
    assert remaining == ["a.csv"]
    assert values == ["cac=500", "currency=EUR"]


def test_extract_flag_present_and_absent():
    remaining, present = _extract_flag(["--check-api"], "--check-api")
    assert remaining == []
    assert present is True

    remaining, present = _extract_flag([], "--check-api")
    assert present is False


# ── the tty gate ─────────────────────────────────────────────────────────


def test_maybe_launch_tui_runs_the_loop_on_a_real_tty(monkeypatch):
    import pragmas_cli.tui as tui_module

    monkeypatch.setattr(tui_module.sys.stdin, "isatty", lambda: True)
    called = {}
    monkeypatch.setattr(tui_module, "run_tui", lambda: called.setdefault("ran", True))
    tui_module.maybe_launch_tui()
    assert called.get("ran") is True


def test_maybe_launch_tui_falls_back_without_a_tty(monkeypatch, capsys):
    import pragmas_cli.tui as tui_module

    monkeypatch.setattr(tui_module.sys.stdin, "isatty", lambda: False)
    tui_module.maybe_launch_tui()
    out = capsys.readouterr().out
    assert "Operational intelligence" in out


# ── local agent mode (Ollama) ───────────────────────────────────────────
# conftest.py's autouse isolated_config stubs local_agent.detect_ollama to
# "nothing found" for every test by default (this dev machine has a real
# Ollama running) — these tests override that stub explicitly per test.


def test_banner_shows_local_command_mode_when_no_ollama(capsys):
    """The default (unmocked-further) case — matches every other test in
    this file, spelled out once explicitly as its own assertion."""
    _run(["/exit"])
    out = capsys.readouterr().out
    assert "Local command mode" in out
    assert "modo programación" in out


def test_banner_shows_local_agent_mode_when_ollama_detected(capsys, monkeypatch):
    monkeypatch.setattr(
        "pragmas_cli.local_agent.detect_ollama",
        lambda *a, **k: [local_agent.OllamaModel("llama3.2:1b", supports_tools=True)],
    )
    _run(["/exit"])
    out = capsys.readouterr().out
    assert "Local agent mode" in out
    assert "llama3.2:1b" in out
    assert "with tool access" in out


def test_banner_notes_chat_only_when_model_lacks_tools(capsys, monkeypatch):
    monkeypatch.setattr(
        "pragmas_cli.local_agent.detect_ollama",
        lambda *a, **k: [local_agent.OllamaModel("phi:latest", supports_tools=False)],
    )
    _run(["/exit"])
    out = capsys.readouterr().out
    # Rich wraps this phrase across a line under capsys's console width
    # (same class of thing as the pre-existing, unrelated data_profile test
    # flake) — normalize whitespace so the assertion doesn't depend on
    # exactly where the wrap lands.
    assert "chat only, no tool access" in " ".join(out.split())


def test_slash_model_no_ollama_is_a_clean_error(capsys):
    _run(["/model", "/exit"])
    err = capsys.readouterr().err
    assert "No Ollama models detected" in err


def test_slash_model_lists_available(capsys, monkeypatch):
    monkeypatch.setattr(
        "pragmas_cli.local_agent.detect_ollama",
        lambda *a, **k: [
            local_agent.OllamaModel("llama3.2:1b", supports_tools=True),
            local_agent.OllamaModel("phi:latest", supports_tools=False),
        ],
    )
    _run(["/model", "/exit"])
    out = capsys.readouterr().out
    assert "llama3.2:1b" in out
    assert "phi:latest" in out


def test_slash_model_switches(capsys, monkeypatch):
    monkeypatch.setattr(
        "pragmas_cli.local_agent.detect_ollama",
        lambda *a, **k: [
            local_agent.OllamaModel("llama3.2:1b", supports_tools=True),
            local_agent.OllamaModel("phi:latest", supports_tools=False),
        ],
    )
    _run(["/model phi:latest", "/exit"])
    out = capsys.readouterr().out
    assert "Switched to phi:latest" in out


def test_slash_model_unknown_name_is_a_clean_error(capsys, monkeypatch):
    monkeypatch.setattr(
        "pragmas_cli.local_agent.detect_ollama",
        lambda *a, **k: [local_agent.OllamaModel("llama3.2:1b", supports_tools=True)],
    )
    _run(["/model does-not-exist", "/exit"])
    err = capsys.readouterr().err
    assert "Unknown model" in err


@respx.mock
def test_free_text_goes_to_chat_and_a_real_tool_call_round_trips(capsys, monkeypatch, cashflow_csv):
    """The end-to-end path: free text -> local_agent chat -> model asks for
    the `inspect` tool -> _run_tool_for_agent really runs /inspect against
    a real CSV via _dispatch (not a stub) -> the captured real output goes
    back to the model as the tool result -> model's final narrated reply
    reaches the human. Verifies the second /api/chat request actually
    carries the real /inspect output, not a placeholder."""
    monkeypatch.setattr(
        "pragmas_cli.local_agent.detect_ollama",
        lambda *a, **k: [local_agent.OllamaModel("llama3.2:1b", supports_tools=True)],
    )
    requests_seen = []

    def _side_effect(request):
        requests_seen.append(json.loads(request.content))
        if len(requests_seen) == 1:
            return httpx.Response(
                200,
                content=_ndjson(
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "inspect",
                                        "arguments": {"csv_path": str(cashflow_csv)},
                                    }
                                }
                            ],
                        },
                        "done": True,
                    }
                ),
            )
        return httpx.Response(200, content=_ndjson({"message": {"content": "Looks like cash flow data."}, "done": True}))

    respx.post(f"{local_agent.DEFAULT_OLLAMA_URL}/api/chat").mock(side_effect=_side_effect)

    _run(["what's in this file for me", "/exit"])
    out = capsys.readouterr().out

    assert len(requests_seen) == 2
    tool_messages = [m for m in requests_seen[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    # Real /inspect output (from tui.py's own Dataset panel), not a stub string.
    assert "Dataset" in tool_messages[0]["content"]
    assert "Rows" in tool_messages[0]["content"]

    assert "running inspect" in out
    assert "Looks like cash flow data." in out


# ── real bugs found by code-review + manual testing against real Ollama ──


def test_build_tool_command_raises_on_missing_or_empty_required_args():
    from pragmas_cli.tui import _DispatchError as DE
    from pragmas_cli.tui import _build_tool_command

    with pytest.raises(DE):
        _build_tool_command("analyze", {"csv_path": "a.csv"})  # no template
    with pytest.raises(DE):
        _build_tool_command("analyze", {"csv_path": "a.csv", "template": ""})  # blank template
    with pytest.raises(DE):
        _build_tool_command("inspect", {})  # no csv_path
    with pytest.raises(DE):
        _build_tool_command("validate_csv", {"csv_path": "a.csv"})  # no template
    with pytest.raises(DE):
        _build_tool_command("market_search", {})  # no topic
    with pytest.raises(DE):
        _build_tool_command("does_not_exist", {})


def test_run_tool_for_agent_reports_missing_template_as_a_clean_usage_error():
    """Regression: a blank --template used to tokenize away entirely,
    letting _extract_option bind the *next* flag's name (--output) as the
    template value instead of raising the intended "missing" error."""
    from pragmas_cli.tui import _run_tool_for_agent

    result = _run_tool_for_agent("analyze", {"csv_path": "a.csv"})
    assert "Usage error" in result
    assert "template" in result


def test_slash_model_switch_resets_conversation_history(monkeypatch):
    import pragmas_cli.tui as tui_module

    monkeypatch.setattr(
        "pragmas_cli.local_agent.detect_ollama",
        lambda *a, **k: [
            local_agent.OllamaModel("llama3.2:1b", supports_tools=True),
            local_agent.OllamaModel("phi:latest", supports_tools=False),
        ],
    )
    _run(["/exit"])  # picks llama3.2:1b at startup
    tui_module._active_session.messages.append({"role": "tool", "content": "leftover from before the switch"})
    assert len(tui_module._active_session.messages) > 1

    tui_module._cmd_model(["phi:latest"])

    assert tui_module._active_session.messages == [{"role": "system", "content": tui_module._SYSTEM_PROMPT}]


def test_slash_model_reselecting_the_same_model_does_not_reset_history(monkeypatch):
    import pragmas_cli.tui as tui_module

    monkeypatch.setattr(
        "pragmas_cli.local_agent.detect_ollama",
        lambda *a, **k: [local_agent.OllamaModel("llama3.2:1b", supports_tools=True)],
    )
    _run(["/exit"])
    tui_module._active_session.messages.append({"role": "user", "content": "keep me"})
    before = list(tui_module._active_session.messages)

    tui_module._cmd_model(["llama3.2:1b"])

    assert tui_module._active_session.messages == before


@respx.mock
def test_chat_turn_falls_back_to_command_mode_on_malformed_stream(capsys, monkeypatch):
    """Regression: a non-httpx error mid-stream (malformed/truncated NDJSON,
    e.g. json.JSONDecodeError) used to escape _run_chat_turn's narrower
    `except httpx.HTTPError`, land in run_tui()'s generic handler instead,
    and leave _active_session set — every next free-text turn then kept
    retrying the same broken connection rather than falling back to
    command-only mode the way a clean connection-refused already did."""
    import pragmas_cli.tui as tui_module

    monkeypatch.setattr(
        "pragmas_cli.local_agent.detect_ollama",
        lambda *a, **k: [local_agent.OllamaModel("llama3.2:1b", supports_tools=True)],
    )
    respx.post(f"{local_agent.DEFAULT_OLLAMA_URL}/api/chat").mock(
        return_value=httpx.Response(200, content=b"not valid ndjson at all\n")
    )

    _run(["hello", "/exit"])
    err = capsys.readouterr().err

    assert "Ollama unreachable" in err
    assert tui_module._active_session is None
