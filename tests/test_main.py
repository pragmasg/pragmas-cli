import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from pragmas_cli.main import app

BASE = "https://api.pragmas.io"
runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Never touch the real ~/.pragmas — every test gets its own tmp config dir."""
    monkeypatch.setenv("PRAGMAS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("PRAGMAS_BASE_URL", BASE)
    monkeypatch.delenv("PRAGMAS_BETA_KEY", raising=False)
    yield tmp_path


# ── login ──────────────────────────────────────────────────────────────


@respx.mock
def test_login_success_saves_key(isolated_config):
    respx.post(f"{BASE}/auth/beta-key").mock(
        return_value=httpx.Response(
            201,
            json={"beta_key": "pk_beta_xyz", "email": "dev@example.com", "created_at": "2026-08-01T00:00:00Z"},
        )
    )
    result = runner.invoke(app, ["login", "--email", "dev@example.com"])
    assert result.exit_code == 0, result.output
    assert "dev@example.com" in result.output

    saved = json.loads((isolated_config / "credentials.json").read_text())
    assert saved["beta_key"] == "pk_beta_xyz"


@respx.mock
def test_login_connection_error_is_friendly(isolated_config):
    respx.post(f"{BASE}/auth/beta-key").mock(side_effect=httpx.ConnectError("refused"))
    result = runner.invoke(app, ["login", "--email", "dev@example.com"])
    assert result.exit_code == 1
    assert "Connection error" in result.output


# ── analyze ────────────────────────────────────────────────────────────


def test_analyze_without_login_prompts_login(isolated_config):
    result = runner.invoke(app, ["analyze", "acme", "--template", "cash_flow_13w"])
    assert result.exit_code == 1
    assert "pragmas login" in result.output


@respx.mock
def test_analyze_json_output(isolated_config, monkeypatch):
    monkeypatch.setenv("PRAGMAS_BETA_KEY", "pk_beta_test")
    respx.post(f"{BASE}/projects/acme/analyze").mock(
        return_value=httpx.Response(
            200,
            json={"success": True, "module": "cash_flow_13w", "results": {"weeks": 13}, "charts": [], "error": None},
        )
    )
    result = runner.invoke(app, ["analyze", "acme", "--template", "cash_flow_13w", "--output", "json"])
    assert result.exit_code == 0, result.output
    assert "cash_flow_13w" in result.output


@respx.mock
def test_analyze_module_failure_exits_nonzero(isolated_config, monkeypatch):
    monkeypatch.setenv("PRAGMAS_BETA_KEY", "pk_beta_test")
    respx.post(f"{BASE}/projects/acme/analyze").mock(
        return_value=httpx.Response(
            200,
            json={"success": False, "module": "cash_flow_13w", "results": {}, "charts": [], "error": "No input CSV"},
        )
    )
    result = runner.invoke(app, ["analyze", "acme", "--template", "cash_flow_13w"])
    assert result.exit_code == 1
    assert "No input CSV" in result.output


# ── market — no login required ──────────────────────────────────────────


@respx.mock
def test_market_works_without_login(isolated_config):
    respx.get(f"{BASE}/market").mock(
        return_value=httpx.Response(
            200,
            json={"topic": "LATAM rates", "summary": "Trending down.", "sources": [], "generated_at": None},
        )
    )
    result = runner.invoke(app, ["market", "LATAM rates"])
    assert result.exit_code == 0, result.output
    assert "Trending down" in result.output


# ── feedback ───────────────────────────────────────────────────────────


def test_feedback_prints_url_without_opening_browser(isolated_config):
    result = runner.invoke(app, ["feedback"])
    assert result.exit_code == 0
    assert "github.com/pragmasg/pragmas-cli/issues" in result.output


# ── v0.2 stubs are honest about not working yet ─────────────────────────


def test_ask_not_available_yet(isolated_config, monkeypatch):
    monkeypatch.setenv("PRAGMAS_BETA_KEY", "pk_beta_test")
    result = runner.invoke(app, ["ask", "what's my EBITDA"])
    assert result.exit_code == 1
    assert "Not available yet" in result.output


def test_tui_not_available_yet(isolated_config):
    result = runner.invoke(app, ["tui"])
    assert result.exit_code == 1
    assert "Not available yet" in result.output


def test_report_generate_not_available_yet(isolated_config, monkeypatch):
    monkeypatch.setenv("PRAGMAS_BETA_KEY", "pk_beta_test")
    result = runner.invoke(app, ["report", "generate", "--project", "acme"])
    assert result.exit_code == 1
    assert "Not available yet" in result.output


# ── misc ──────────────────────────────────────────────────────────────


def test_version_flag(isolated_config):
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "pragmas-cli" in result.output
