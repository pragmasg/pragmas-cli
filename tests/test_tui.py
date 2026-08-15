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
import httpx
import pytest
import respx

from pragmas_cli.tui import (
    _DispatchError,
    _extract_flag,
    _extract_option,
    _extract_repeated,
    run_tui,
)

BASE = "https://api.pragmas.io"


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
    _run(["what's my burn rate this month", "/exit"])
    out = capsys.readouterr().out
    assert "Not a chat agent" in out


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
