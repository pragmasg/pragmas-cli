import csv
import errno
import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from pragmas_cli.main import app, main

BASE = "https://api.pragmas.io"
runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Never touch the real ~/.pragmas — every test gets its own tmp config dir."""
    monkeypatch.setenv("PRAGMAS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("PRAGMAS_BASE_URL", BASE)
    monkeypatch.delenv("PRAGMAS_BETA_KEY", raising=False)
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


# ── analyze — local, no login required ──────────────────────────────────


def test_analyze_runs_without_login(isolated_config, cashflow_csv, tmp_path):
    """No PRAGMAS_BETA_KEY set anywhere — proves analyze doesn't need one."""
    result = runner.invoke(
        app,
        ["analyze", str(cashflow_csv), "--template", "cash_flow_13w",
         "--output", "json", "--output-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == 0, result.output
    assert "cash_flow_13w" in result.output


def test_analyze_nonexistent_file_rejected_by_cli(isolated_config, tmp_path):
    result = runner.invoke(app, ["analyze", str(tmp_path / "nope.csv"), "--template", "cash_flow_13w"])
    assert result.exit_code != 0


def test_analyze_unknown_template_exits_nonzero(isolated_config, cashflow_csv):
    result = runner.invoke(app, ["analyze", str(cashflow_csv), "--template", "not_a_real_template"])
    assert result.exit_code == 1
    assert "Unknown module" in result.output


def test_analyze_table_output_shows_metrics(isolated_config, cashflow_csv):
    result = runner.invoke(app, ["analyze", str(cashflow_csv), "--template", "cash_flow_13w"])
    assert result.exit_code == 0, result.output
    assert "min_balance" in result.output
    assert "Charts written" in result.output


def test_analyze_table_output_summarizes_nested_values(isolated_config, cashflow_csv):
    """cash_flow_13w's `weeks` is a list of 13 dicts — dumping it raw makes
    the table unreadable. Table mode should summarize it and point at
    --output json instead."""
    result = runner.invoke(app, ["analyze", str(cashflow_csv), "--template", "cash_flow_13w"])
    assert result.exit_code == 0, result.output
    assert "13 items" in result.output
    assert "--output json" in result.output
    assert "'week_start'" not in result.output  # the raw dump is gone


# ── inspect — local, no login required ──────────────────────────────────


@pytest.fixture
def saas_csv(tmp_path):
    path = tmp_path / "saas.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_id", "month", "mrr"])
        writer.writerows([
            ["cust_1", "2026-07", 1000],
            ["cust_2", "2026-07", 2000],
            ["cust_3", "2026-08", 3000],
            ["cust_4", "2026-08", 2000],
            ["cust_5", "2026-09", 1000],
            ["cust_6", "2026-09", 3000],
        ])
    return path


def test_inspect_dataset_summary(isolated_config, saas_csv):
    result = runner.invoke(app, ["inspect", str(saas_csv)])
    assert result.exit_code == 0, result.output
    assert "Dataset" in result.output
    assert "Rows" in result.output
    assert "6" in result.output
    assert "Columns" in result.output
    assert "3" in result.output
    assert "Size" in result.output


def test_inspect_detects_column_types(isolated_config, saas_csv):
    result = runner.invoke(app, ["inspect", str(saas_csv)])
    assert result.exit_code == 0, result.output
    assert "customer_id" in result.output
    assert "id-like" in result.output
    assert "month" in result.output
    assert "date-like" in result.output
    assert "mrr" in result.output
    assert "numeric" in result.output


def test_inspect_suggests_matching_template(isolated_config, saas_csv):
    """customer_id/month/mrr is a subset of saas_metrics' REQUIRED_COLS, so
    it must be suggested — and cash_flow_13w/ecommerce_unit_economics (which
    need date/concept/amount etc.) must not."""
    result = runner.invoke(app, ["inspect", str(saas_csv)])
    assert result.exit_code == 0, result.output
    assert "Potential templates" in result.output
    assert "saas_metrics" in result.output
    assert "cash_flow_13w" not in result.output
    assert "ecommerce_unit_economics" not in result.output


def test_inspect_no_match_clean_message(isolated_config, tmp_path):
    path = tmp_path / "unrelated.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("foo,bar\n1,2\n")
    result = runner.invoke(app, ["inspect", str(path)])
    assert result.exit_code == 0, result.output
    assert "No potential template matches" in result.output


def test_inspect_empty_file_is_honest(isolated_config, tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    result = runner.invoke(app, ["inspect", str(path)])
    assert result.exit_code == 0, result.output
    assert "No columns found (empty file?)" in result.output


def test_inspect_header_only_reports_no_sample_data(isolated_config, tmp_path):
    path = tmp_path / "header_only.csv"
    path.write_text("customer_id,month,mrr\n", encoding="utf-8")
    result = runner.invoke(app, ["inspect", str(path)])
    assert result.exit_code == 0, result.output
    assert "unknown (no data in sample)" in result.output


def test_inspect_nonexistent_file_rejected_by_cli(isolated_config, tmp_path):
    result = runner.invoke(app, ["inspect", str(tmp_path / "nope.csv")])
    assert result.exit_code != 0


# ── market — local, no login required ───────────────────────────────────


def test_market_works_without_login(isolated_config, monkeypatch):
    class _FakeDDGS:
        def text(self, query, max_results=5):
            return [{"title": "Reuters", "href": "https://example.com", "body": "Trending down."}]

    monkeypatch.setattr("ddgs.DDGS", _FakeDDGS)
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


# ── main() swallows a truncated-pipe reader, doesn't traceback ─────────


def test_main_swallows_broken_pipe_error(monkeypatch):
    def _raise(*a, **k):
        raise BrokenPipeError()

    monkeypatch.setattr("pragmas_cli.main.app", _raise)
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0


def test_main_swallows_windows_legacy_console_pipe_error(monkeypatch):
    def _raise(*a, **k):
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr("pragmas_cli.main.app", _raise)
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0


def test_main_reraises_unrelated_os_error(monkeypatch):
    def _raise(*a, **k):
        raise OSError(errno.ENOENT, "No such file or directory")

    monkeypatch.setattr("pragmas_cli.main.app", _raise)
    with pytest.raises(OSError):
        main()


# ── misc ──────────────────────────────────────────────────────────────


def test_version_flag(isolated_config):
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "pragmas-cli" in result.output


def test_no_args_shows_welcome_not_bare_help(isolated_config):
    """Regression: used to be no_args_is_help=True (plain command list).
    Now it's a banner + static quick-start/environment panels."""
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Quick start" in result.output
    assert "Environment" in result.output
    assert "Rscript" in result.output
    assert "pragmas analyze" in result.output


def test_help_flag_still_lists_commands(isolated_config):
    """--help must still work independently of the no-args welcome screen."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "analyze" in result.output
    assert "market" in result.output
