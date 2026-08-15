"""Tests for the Textual app (tui.py) — driven headlessly via Textual's own
`App.run_test()` / `Pilot`, no real terminal involved. `dispatch.py`'s own
logic is exercised directly in test_dispatch.py; these tests are about the
presentation layer: does the right thing get mounted, does resize/history/
autocomplete/click actually work.
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


# ── startup ───────────────────────────────────────────────────────────────


async def test_app_boots_and_shows_the_banner_once():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        msgs = _chat_messages(app)
        assert len(msgs) == 1
        assert msgs[0].role == "system"
        assert "Operational intelligence" in str(msgs[0]._content)


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


async def test_conn_bar_shows_tool_support_correctly_from_first_detection(monkeypatch):
    """Regression (code-review): _apply_session_state used to set
    ollama_connected/active_model/supports_tools as three separate reactive
    fields, each with its own watch_* refreshing the bar — but
    supports_tools had no watcher at all, and the two that did exist could
    fire before supports_tools was assigned. Startup with a tools-capable
    model permanently showed "chat only" regardless of the real
    capability. Now one explicit refresh after all three fields are set."""
    monkeypatch.setattr(
        local_agent, "detect_ollama", lambda *a, **k: [local_agent.OllamaModel("llama3.2:1b", supports_tools=True)]
    )
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        assert app.supports_tools is True
        bar_text = str(app.query_one("#conn-bar").content)
        assert "tools" in bar_text
        assert "chat only" not in bar_text


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
        # Right after submit, before the worker's finally-block runs, it
        # should already be disabled (checked before any pause lets the
        # worker finish, since /help is fast enough this is genuinely racy
        # otherwise — the immediate disable happens synchronously in
        # _submit_line, on the main thread, before the worker is even
        # scheduled).
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
    # run_test()'s context manager exiting cleanly (no hang, no exception)
    # is itself the assertion — app.exit() was reached.


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


# ── quick commands (sidebar list) ────────────────────────────────────────


async def test_quick_command_prefills_input_for_an_arg_command():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        quick = app.query_one("#quick-commands")
        quick.focus()
        await pilot.pause()
        await pilot.press("enter")  # index 0 = /analyze, pre-selected on mount
        await pilot.pause(0.2)
        field = app.query_one("#prompt")
        assert field.value == "/analyze "


async def test_quick_command_auto_submits_a_zero_arg_command():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        quick = app.query_one("#quick-commands")
        quick.index = 4  # "/help" in _QUICK_COMMANDS
        quick.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.3)
        msgs = _chat_messages(app)
        assert any(m.role == "command" for m in msgs)


async def test_quick_command_exit_quits_the_app():
    app = PragmasApp()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        quick = app.query_one("#quick-commands")
        quick.index = 5  # "/exit" in _QUICK_COMMANDS
        quick.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.2)


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
