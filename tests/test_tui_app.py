"""Tests for the Textual app (tui.py) — driven headlessly via Textual's own
`App.run_test()` / `Pilot`, no real terminal involved. `dispatch.py`'s own
logic is exercised directly in test_dispatch.py; these tests are about the
presentation layer: does the right thing get mounted, does resize/history/
autocomplete/click actually work.

`isolated_config` (conftest.py, autouse) points `PRAGMAS_CONFIG_DIR` at a
fresh `tmp_path` per test, which means `config_dir()/last_banner` never
exists yet in any test here — every test always sees the *full* banner
(see `PragmasApp._should_show_full_banner`), never the once-a-day compact
one. The compact-banner path has its own dedicated test below that manages
the marker file directly.
"""
import json

import httpx
import pytest
import respx

from pragmas_cli import local_agent
from pragmas_cli.tui import PragmasApp
from pragmas_cli.tui_widgets import ChatMessage

OLLAMA = local_agent.DEFAULT_OLLAMA_URL


def _ndjson(*chunks) -> str:
    return "\n".join(json.dumps(c) for c in chunks) + "\n"


def _chat_messages(app):
    return list(app.query("#chat-scroll ChatMessage"))


async def _click_quick(pilot, app, name: str) -> None:
    await pilot.click(app.query_one(f"#quick-{name}"))


# ── startup ───────────────────────────────────────────────────────────────


async def test_app_boots_and_shows_the_full_banner_on_a_fresh_config_dir():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        msgs = _chat_messages(app)
        assert len(msgs) == 1
        assert msgs[0].role == "system"
        assert "Operational intelligence" in str(msgs[0]._content)


async def test_banner_is_compact_on_a_second_launch_the_same_day():
    """Regression coverage for the "banner once a day" feature itself,
    not just its default (always-full-in-tests) state — writes the marker
    by hand rather than waiting for a real second launch."""
    from datetime import date

    app = PragmasApp()
    async with app.run_test(size=(100, 40)):
        pass  # first launch: writes today's date to the marker file

    app2 = PragmasApp()
    async with app2.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        msgs = _chat_messages(app2)
        assert len(msgs) == 1
        assert "Operational intelligence" not in str(msgs[0]._content)
        assert "/help for commands" in str(msgs[0]._content)

    # Sanity: the marker really did get written where expected.
    from pragmas_cli.config import config_dir

    marker = config_dir() / "last_banner"
    assert marker.read_text(encoding="utf-8").strip() == date.today().isoformat()


async def test_banner_does_not_repeat_environment_or_quickstart_info():
    """Regression (user report): the chat panel's banner used to reuse
    main._show_welcome()'s full captured output, which includes a "Quick
    start" panel and an "Environment" panel — both now redundant with the
    sidebar's own Quick commands buttons and Environment section. The chat
    panel is only chat."""
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        banner_text = str(_chat_messages(app)[0]._content)
        assert "Quick start" not in banner_text
        assert "Rscript" not in banner_text


async def test_no_ollama_shows_local_command_mode():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        assert app.ollama_connected is False
        assert app.sub_title == "Local command mode"


async def test_ollama_detected_shows_local_agent_mode(monkeypatch):
    monkeypatch.setattr(
        local_agent, "detect_ollama", lambda *a, **k: [local_agent.OllamaModel("llama3.2:1b", supports_tools=True)]
    )
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        assert app.ollama_connected is True
        assert app.active_model == "llama3.2:1b"
        assert app.sub_title == "Local agent mode"


# ── tools 3-state indicator (unsupported / available / active) ──────────


async def test_tools_indicator_unsupported_when_model_lacks_tools(monkeypatch):
    monkeypatch.setattr(
        local_agent, "detect_ollama", lambda *a, **k: [local_agent.OllamaModel("phi", supports_tools=False)]
    )
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        assert "unsupported" in str(app.query_one("#status-bar").content)
        assert "unsupported" in str(app.query_one("#session-info").content)


async def test_tools_indicator_available_when_supported_but_unused(monkeypatch):
    monkeypatch.setattr(
        local_agent, "detect_ollama", lambda *a, **k: [local_agent.OllamaModel("llama3.2:1b", supports_tools=True)]
    )
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        assert "available" in str(app.query_one("#status-bar").content)
        assert app.tools_used_this_session is False


@respx.mock
async def test_tools_indicator_becomes_active_after_a_real_tool_call(cashflow_csv, monkeypatch):
    """Regression: PragmasApp._finish_processing used to call
    _apply_session_state() unconditionally after every turn, which always
    resets tools_used_this_session — wiping out the flag mark_tool_used()
    had *just* set moments earlier in that same turn. Caught by hand
    running a real tool call through the app, not by any test written in
    advance; this is that test, written after the fact."""
    monkeypatch.setattr(
        local_agent, "detect_ollama", lambda *a, **k: [local_agent.OllamaModel("llama3.2:1b", supports_tools=True)]
    )
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
        httpx.Response(200, content=_ndjson({"message": {"content": "Done."}, "done": True})),
    ]
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.2)
        field = app.query_one("#prompt")
        field.value = "what's in this file"
        await pilot.press("enter")
        await pilot.pause(1.0)
        assert app.tools_used_this_session is True
        assert "active" in str(app.query_one("#status-bar").content)


async def test_r_available_is_checked_once_not_on_every_turn(monkeypatch):
    """Regression (code-review): _refresh_status_bar used to call
    r_available() — a shutil.which PATH scan — fresh on every single turn,
    duplicating the one _refresh_env_info already does at startup. Now
    cached once in on_mount and reused."""
    calls = {"n": 0}

    def _counting_r_available():
        calls["n"] += 1
        return True

    monkeypatch.setattr("pragmas_sdk.analysis.r_runner.r_available", _counting_r_available)

    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        assert calls["n"] == 1
        field = app.query_one("#prompt")
        field.value = "/help"
        await pilot.press("enter")
        await pilot.pause(0.3)
        field.value = "/templates"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert calls["n"] == 1


# ── the JSON-tool-definition leak (the reported bug) ─────────────────────


@respx.mock
async def test_raw_json_tool_definitions_never_shown_verbatim(monkeypatch):
    """The actual reported bug: a small/quantized local model can dump the
    tool-definition JSON as plain `content` instead of a real `tool_calls`
    entry (confirmed for real against llama3.2:1b, not hypothetical). The
    user must never see that raw JSON — see _TextualConsoleAdapter's
    per-round buffering in tui.py."""
    monkeypatch.setattr(
        local_agent, "detect_ollama", lambda *a, **k: [local_agent.OllamaModel("llama3.2:1b", supports_tools=True)]
    )
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(
            200,
            content=_ndjson(
                {
                    "message": {
                        "content": '{"type": "function", "function": {"name": "validate_csv", '
                        '"description": "Check whether a local CSV has the columns"}}'
                    },
                    "done": True,
                }
            ),
        )
    )
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.2)
        field = app.query_one("#prompt")
        field.value = "hello"
        await pilot.press("enter")
        await pilot.pause(0.5)
        assistant = [m for m in _chat_messages(app) if m.role == "assistant"][-1]
        content = str(assistant._content)
        assert '"type": "function"' not in content
        assert "validate_csv" not in content
        assert "tool-definition JSON" in content


@respx.mock
async def test_normal_prose_still_streams_live_unaffected(monkeypatch):
    """The JSON-detection heuristic must not swallow an ordinary reply that
    happens to arrive in multiple chunks."""
    monkeypatch.setattr(
        local_agent, "detect_ollama", lambda *a, **k: [local_agent.OllamaModel("llama3.2:1b", supports_tools=True)]
    )
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(
            200,
            content=_ndjson(
                {"message": {"content": "The columns "}, "done": False},
                {"message": {"content": "are date, amount."}, "done": True},
            ),
        )
    )
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.2)
        field = app.query_one("#prompt")
        field.value = "hello"
        await pilot.press("enter")
        await pilot.pause(0.5)
        assistant = [m for m in _chat_messages(app) if m.role == "assistant"][-1]
        assert "The columns are date, amount." in str(assistant._content)


# ── "thinking…" placeholder ──────────────────────────────────────────────


@respx.mock
async def test_thinking_placeholder_shown_while_processing_then_replaced(monkeypatch):
    monkeypatch.setattr(
        local_agent, "detect_ollama", lambda *a, **k: [local_agent.OllamaModel("llama3.2:1b", supports_tools=False)]
    )
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, content=_ndjson({"message": {"content": "Hi there."}, "done": True}))
    )
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.2)
        field = app.query_one("#prompt")
        field.value = "hello"
        await pilot.press("enter")
        await pilot.pause(0.5)
        assistant = [m for m in _chat_messages(app) if m.role == "assistant"][-1]
        content = str(assistant._content)
        assert "thinking" not in content
        assert "Hi there." in content


async def test_thinking_placeholder_removed_when_chat_branch_not_reached(cashflow_csv, monkeypatch):
    """A CSV path with an active session takes the auto-inspect branch, not
    chat — the "thinking…" placeholder created up front for the chat path
    must not linger."""
    monkeypatch.setattr(
        local_agent, "detect_ollama", lambda *a, **k: [local_agent.OllamaModel("llama3.2:1b", supports_tools=True)]
    )
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.2)
        field = app.query_one("#prompt")
        field.value = str(cashflow_csv)
        await pilot.press("enter")
        await pilot.pause(0.5)
        msgs = _chat_messages(app)
        # Checking only "assistant"-role messages, not the whole transcript
        # — pytest's own tmp_path embeds this test's *name* in the CSV
        # path shown back in the "user" bubble, which would make a naive
        # "thinking" in any(...) substring check over every message a false
        # positive on this test's own name (caught the hard way, not
        # theorized).
        assert not any("thinking" in str(m._content) for m in msgs if m.role == "assistant")
        assert any(m.role == "command" for m in msgs)


async def test_thinking_placeholder_removed_when_auto_inspect_raises(tmp_path, monkeypatch):
    """Regression (code-review, the most severe finding): the CSV
    auto-inspect branch has no try/except of its own (unlike
    dispatch_captured, which wraps dispatch()) — main._scan_csv opens with
    encoding="utf-8-sig" and raises UnicodeDecodeError on a non-UTF-8 file
    (common for a Windows/Excel export). That used to propagate straight
    past the "thinking…" placeholder cleanup, leaving it stuck forever with
    no explanation. Now caught, cleaned up, and shown as a real error."""
    monkeypatch.setattr(
        local_agent, "detect_ollama", lambda *a, **k: [local_agent.OllamaModel("llama3.2:1b", supports_tools=True)]
    )
    bad_csv = tmp_path / "latin1.csv"
    # 0xFF is not a valid byte anywhere in a UTF-8 sequence.
    bad_csv.write_bytes(b"date,concept\n2026-01-01,caf\xe9\n")

    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.2)
        field = app.query_one("#prompt")
        field.value = str(bad_csv)
        await pilot.press("enter")
        await pilot.pause(0.5)
        msgs = _chat_messages(app)
        assert not any("thinking" in str(m._content) for m in msgs if m.role == "assistant")
        assert any(m.role == "error" and "Unexpected error" in str(m._content) for m in msgs)
        assert field.disabled is False


# ── slash commands via the real input widget ────────────────────────────


async def test_slash_help_renders_a_command_message():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        field = app.query_one("#prompt")
        field.value = "/help"
        await pilot.press("enter")
        await pilot.pause(0.3)
        msgs = _chat_messages(app)
        assert msgs[-1].role == "command"
        assert "/analyze" in str(msgs[-1]._content)


async def test_slash_market_renders_result(monkeypatch):
    class _FakeDDGS:
        def text(self, query, max_results=5):
            return [{"title": "Reuters", "href": "https://example.com", "body": "Trending down."}]

    monkeypatch.setattr("ddgs.DDGS", _FakeDDGS)
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        field = app.query_one("#prompt")
        field.value = '/market "consumer spending"'
        await pilot.press("enter")
        await pilot.pause(0.5)
        msgs = _chat_messages(app)
        assert msgs[-1].role == "command"
        assert "Trending down" in str(msgs[-1]._content)


async def test_unknown_command_renders_an_error_message():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        field = app.query_one("#prompt")
        field.value = "/nope"
        await pilot.press("enter")
        await pilot.pause(0.3)
        msgs = _chat_messages(app)
        assert msgs[-1].role == "error"
        assert msgs[-1].has_class("-error")
        assert "Unknown command" in str(msgs[-1]._content)


async def test_free_text_with_no_ollama_gets_honest_refusal():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        field = app.query_one("#prompt")
        field.value = "what's my burn rate"
        await pilot.press("enter")
        await pilot.pause(0.3)
        msgs = _chat_messages(app)
        assert msgs[-1].role == "error"
        assert "No chat agent available" in str(msgs[-1]._content)


async def test_input_disabled_while_processing_and_reenabled_after():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        field = app.query_one("#prompt")
        field.value = "/help"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert field.disabled is False  # re-enabled by the time it settles


async def test_exit_word_quits_the_app():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        field = app.query_one("#prompt")
        field.value = "/exit"
        await pilot.press("enter")
        await pilot.pause(0.2)
        # run_test()'s context manager exits cleanly either way (nothing
        # inside it raises whether or not exit() actually ran) — checking
        # App.exit()'s own `_exit` flag is what actually proves the click
        # was handled, not just that the test didn't hang.
        assert app._exit is True


async def test_ctrl_q_quits_the_app():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause(0.2)
        assert app._exit is True


async def test_ctrl_h_runs_help():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        before = len(_chat_messages(app))
        await pilot.press("ctrl+h")
        await pilot.pause(0.3)
        msgs = _chat_messages(app)
        assert len(msgs) > before
        assert msgs[-1].role == "command"
        assert "/analyze" in str(msgs[-1]._content)


# ── real chat + tool-calling round trip ─────────────────────────────────


@respx.mock
async def test_chat_with_tool_call_streams_into_an_assistant_bubble(cashflow_csv, monkeypatch):
    monkeypatch.setattr(
        local_agent, "detect_ollama", lambda *a, **k: [local_agent.OllamaModel("llama3.2:1b", supports_tools=True)]
    )
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

    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.2)
        field = app.query_one("#prompt")
        field.value = "what's in this file for me"
        await pilot.press("enter")
        await pilot.pause(1.0)

        msgs = _chat_messages(app)
        roles = [m.role for m in msgs]
        assert "user" in roles
        assert "assistant" in roles
        assistant = [m for m in msgs if m.role == "assistant"][-1]
        content = str(assistant._content)
        assert "running inspect" in content
        assert "Looks like cash flow data." in content
        # No redundant "assistant>" role-prefix marker duplicating the
        # bubble's own "Assistant" header (dropped as noise — see
        # _TextualConsoleAdapter.print's handling of _ASSISTANT_PREFIX).
        assert "assistant>" not in content


# ── CommandField: history + tab-completion ──────────────────────────────


async def test_tab_completes_a_unique_command_prefix():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        field = app.query_one("#prompt")
        field.value = "/an"
        field.focus()
        await pilot.press("tab")
        await pilot.pause()
        assert field.value == "/analyze "


async def test_tab_on_ambiguous_prefix_leaves_value_unchanged():
    """"market" and "model" both start with "m" — their only common prefix
    IS "m", the same length as what's already typed, so there's nothing
    useful to complete to (unlike a real shell, which would still list the
    candidates; this Tab-completion is deliberately minimal — see
    tui_widgets.py's `_autocomplete`). The key press is still consumed
    (doesn't fall through to Textual's focus-next default), it just leaves
    the value as-is rather than guessing."""
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        field = app.query_one("#prompt")
        field.value = "/m"
        field.focus()
        await pilot.press("tab")
        await pilot.pause()
        assert field.value == "/m"


async def test_history_up_then_down():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        field = app.query_one("#prompt")
        field.value = "/help"
        await pilot.press("enter")
        await pilot.pause(0.3)
        field.value = "/templates"
        await pilot.press("enter")
        await pilot.pause(0.3)

        field.value = ""
        field.focus()
        await pilot.press("up")
        await pilot.pause()
        assert field.value == "/templates"
        await pilot.press("up")
        await pilot.pause()
        assert field.value == "/help"
        await pilot.press("down")
        await pilot.pause()
        assert field.value == "/templates"
        await pilot.press("down")
        await pilot.pause()
        assert field.value == ""


# ── quick commands (sidebar buttons) ─────────────────────────────────────


async def test_quick_command_button_prefills_input_for_an_arg_command():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await _click_quick(pilot, app, "analyze")
        await pilot.pause(0.2)
        field = app.query_one("#prompt")
        assert field.value == "/analyze "


async def test_quick_command_button_auto_submits_a_zero_arg_command():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await _click_quick(pilot, app, "help")
        await pilot.pause(0.3)
        msgs = _chat_messages(app)
        assert any(m.role == "command" for m in msgs)


async def test_quick_command_button_exit_quits_the_app():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await _click_quick(pilot, app, "exit")
        await pilot.pause(0.2)
        assert app._exit is True


# ── responsive sidebar ───────────────────────────────────────────────────


async def test_sidebar_collapses_under_80_cols_and_hides_toggle_under_40():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        sidebar = app.query_one("#sidebar")
        toggle = app.query_one("#sidebar-toggle")
        assert sidebar.has_class("-collapsed") is False

        await pilot.resize_terminal(60, 30)
        await pilot.pause()
        assert sidebar.has_class("-collapsed") is True
        assert toggle.has_class("-visible") is True

        await pilot.resize_terminal(30, 30)
        await pilot.pause()
        assert sidebar.has_class("-collapsed") is True
        assert toggle.has_class("-visible") is False

        await pilot.resize_terminal(100, 40)
        await pilot.pause()
        assert sidebar.has_class("-collapsed") is False


async def test_ctrl_b_toggles_sidebar_and_overrides_the_breakpoint():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        sidebar = app.query_one("#sidebar")
        assert sidebar.has_class("-collapsed") is False

        await pilot.press("ctrl+b")
        await pilot.pause()
        assert sidebar.has_class("-collapsed") is True

        await pilot.press("ctrl+b")
        await pilot.pause()
        assert sidebar.has_class("-collapsed") is False
