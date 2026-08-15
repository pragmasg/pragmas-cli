"""Fixtures shared across test_main.py and test_tui.py."""
import csv

import pytest

BASE = "https://api.pragmas.io"


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
