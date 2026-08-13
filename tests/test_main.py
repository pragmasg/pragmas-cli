import csv
import errno
import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from pragmas_cli.main import app, main, _coerce_param_value, _parse_params

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


@pytest.fixture
def saas_metrics_csv(tmp_path):
    """2 customers x 3 months, some churn/expansion — enough for
    cac_payback_months/ltv_cac_ratio to be non-null so --param cac=... is
    observable in the output."""
    path = tmp_path / "saas.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_id", "month", "mrr"])
        writer.writerows([
            ["cust_1", "2026-05", 100],
            ["cust_1", "2026-06", 100],
            ["cust_1", "2026-07", 120],
            ["cust_2", "2026-05", 200],
            ["cust_2", "2026-06", 200],
            ["cust_2", "2026-07", 200],
            ["cust_3", "2026-05", 50],
            ["cust_3", "2026-06", 50],
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


# ── analyze --param ──────────────────────────────────────────────────────


def test_param_cac_changes_saas_metrics_result(isolated_config, saas_metrics_csv, tmp_path):
    """Real end-to-end: passing --param cac=500 must change the CAC-dependent
    fields compared to not passing it, proving params are actually wired
    through to the SDK, not just accepted and dropped."""
    baseline = runner.invoke(
        app,
        ["analyze", str(saas_metrics_csv), "--template", "saas_metrics",
         "--output", "json", "--output-dir", str(tmp_path / "out1")],
    )
    assert baseline.exit_code == 0, baseline.output
    baseline_data = json.loads(baseline.output)
    assert baseline_data["results"]["cac_payback_months"] is None  # no cac given

    with_cac = runner.invoke(
        app,
        ["analyze", str(saas_metrics_csv), "--template", "saas_metrics",
         "--param", "cac=500",
         "--output", "json", "--output-dir", str(tmp_path / "out2")],
    )
    assert with_cac.exit_code == 0, with_cac.output
    with_cac_data = json.loads(with_cac.output)
    assert with_cac_data["results"]["cac_payback_months"] is not None
    assert with_cac_data["results"]["ltv_cac_ratio"] is not None
    assert with_cac_data["results"]["cac"] == 500


def test_param_unknown_key_no_warning_when_template_has_no_known_params(isolated_config, saas_metrics_csv):
    """saas_metrics predates the KNOWN_PARAMS convention (known is None) —
    the CLI must skip the unknown-param check silently rather than guess."""
    result = runner.invoke(
        app,
        ["analyze", str(saas_metrics_csv), "--template", "saas_metrics", "--param", "currency=EUR"],
    )
    assert result.exit_code == 0, result.output
    assert "Warning" not in result.output
    assert "not a recognized param" not in result.output


def test_param_malformed_exits_cleanly_not_traceback(isolated_config, cashflow_csv):
    result = runner.invoke(
        app,
        ["analyze", str(cashflow_csv), "--template", "cash_flow_13w", "--param", "foo"],
    )
    assert result.exit_code == 1
    assert "Invalid --param" in result.output
    assert "key=value" in result.output
    assert "Traceback" not in result.output


def test_param_value_containing_equals_is_split_on_first_equals_only(isolated_config, cashflow_csv):
    """A value that legitimately contains '=' (e.g. a query string) must not
    be mangled by a naive split("=")."""
    parsed = _parse_params(["url=https://x.test/?a=1&b=2"])
    assert parsed == {"url": "https://x.test/?a=1&b=2"}


def test_coerce_param_value_int():
    assert _coerce_param_value("1") == 1
    assert isinstance(_coerce_param_value("1"), int)


def test_coerce_param_value_float():
    assert _coerce_param_value("1.5") == 1.5
    assert isinstance(_coerce_param_value("1.5"), float)


def test_coerce_param_value_bool_lowercase_only():
    assert _coerce_param_value("true") is True
    assert _coerce_param_value("false") is False


def test_coerce_param_value_leaves_yes_no_and_mixed_case_as_strings():
    assert _coerce_param_value("yes") == "yes"
    assert _coerce_param_value("no") == "no"
    assert _coerce_param_value("True") == "True"
    assert _coerce_param_value("False") == "False"


# ── market — local, no login required ───────────────────────────────────


def test_market_works_without_login(isolated_config, monkeypatch):
    class _FakeDDGS:
        def text(self, query, max_results=5):
            return [{"title": "Reuters", "href": "https://example.com", "body": "Trending down."}]

    monkeypatch.setattr("ddgs.DDGS", _FakeDDGS)
    result = runner.invoke(app, ["market", "LATAM rates"])
    assert result.exit_code == 0, result.output
    assert "Trending down" in result.output


# ── templates — local, no login required ────────────────────────────────


EXPECTED_TEMPLATE_NAMES = [
    "cash_flow_13w",
    "saas_metrics",
    "ecommerce_unit_economics",
    "r:seasonality",
    "r:outliers",
    "r:correlations",
]


def test_templates_lists_all_known_templates(isolated_config):
    result = runner.invoke(app, ["templates"])
    assert result.exit_code == 0, result.output
    for name in EXPECTED_TEMPLATE_NAMES:
        assert name in result.output


def test_templates_each_has_a_real_description(isolated_config):
    """Every row needs an actual one-line description pulled from real
    source (module docstring / R header comment), not a blank cell or a
    fabricated placeholder."""
    result = runner.invoke(app, ["templates"])
    assert result.exit_code == 0, result.output
    output = result.output

    assert "cash flow projection" in output
    assert "SaaS metrics" in output
    assert "Unit economics for e-commerce" in output
    assert "seasonality" in output
    assert "outlier detection" in output
    assert "correlation matrix" in output


def test_templates_hints_at_show_subcommand(isolated_config):
    result = runner.invoke(app, ["templates"])
    assert result.exit_code == 0, result.output
    assert "pragmas templates show" in result.output


def test_templates_python_descriptions_are_not_blank(isolated_config):
    from pragmas_cli.main import _python_template_description
    from pragmas_sdk.analysis import MODULES

    for name in MODULES:
        desc = _python_template_description(name)
        assert desc.strip() != ""


def test_templates_r_descriptions_are_not_blank(isolated_config):
    from pragmas_cli.main import _r_template_description
    from pragmas_sdk.analysis import R_TEMPLATES

    for name in R_TEMPLATES:
        desc = _r_template_description(name)
        assert desc.strip() != ""
        assert desc != "R-backed statistical template (see pragmas-sdk source for details)."


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
