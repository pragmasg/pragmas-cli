"""Fixtures shared across test_main.py, test_dispatch.py, and test_tui_app.py."""
import csv

import pytest

import pragmas_cli.dispatch as dispatch

BASE = "https://api.pragmas.io"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Never touch the real ~/.pragmas — every test gets its own tmp config dir.

    Also stubs out Ollama detection to "nothing found": this dev machine has
    a real Ollama running, and every test that reaches run_tui() now probes
    for one at startup (local agent mode) — without this, tests would
    nondeterministically pick up real local models instead of the
    "no Ollama" baseline. Tried pinning PRAGMAS_OLLAMA_URL at a closed port
    first (127.0.0.1:1) expecting an instant connection-refused; on Windows
    that silently sat until the full timeout instead (~1.5s × every test
    hitting run_tui() ≈ the suite going from ~9s to ~50s) — monkeypatching
    the detection call itself is what actually avoids any real socket, on
    any OS. Tests that want agent mode override this one attribute again
    with their own monkeypatch (last one wins, same pattern as any other
    fixture override), same as PRAGMAS_BASE_URL/BETA_KEY below.
    """
    monkeypatch.setenv("PRAGMAS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("PRAGMAS_BASE_URL", BASE)
    monkeypatch.delenv("PRAGMAS_BETA_KEY", raising=False)
    monkeypatch.setattr("pragmas_cli.local_agent.detect_ollama", lambda *a, **k: [])
    # This dev machine's real Ollama also has OLLAMA_HOST set (to 0.0.0.0,
    # for LAN access) — found the hard way: a test mocked a chat request
    # against local_agent.DEFAULT_OLLAMA_URL while ollama_base_url() was
    # silently reading this ambient env var and building a *different*
    # base_url, so respx correctly reported "not mocked" and the test just
    # saw 0 requests with no assertion catching why. Clearing both env vars
    # here is what actually makes ollama_base_url() deterministic in tests;
    # PRAGMAS_OLLAMA_URL/OLLAMA_HOST alone (without also stubbing
    # detect_ollama above) was tried first and wasn't enough on its own.
    monkeypatch.delenv("PRAGMAS_OLLAMA_URL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    # dispatch._active_session is module-level global state — the old
    # Rich-REPL run_tui() used to reset it at the top of every call, so
    # tests driving that loop always started clean for free. Now that
    # nothing resets it automatically (only the Textual app's on_mount, or
    # an explicit dispatch.start_new_session()/cmd_model call does), a test
    # that sets a session and doesn't clean up would otherwise leak into
    # whichever test runs next in the same pytest process.
    monkeypatch.setattr(dispatch, "_active_session", None)
    yield tmp_path


@pytest.fixture
def cashflow_csv(tmp_path):
    path = tmp_path / "cash.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "concept", "amount"])
        writer.writerows([
            ["2026-07-06", "customer A payment", 10000],
            ["2026-07-15", "payroll", -12000],
            ["2026-07-21", "customer B payment", 5000],
        ])
    return path
