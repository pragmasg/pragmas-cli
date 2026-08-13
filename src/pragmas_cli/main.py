"""pragmas — PRAGMAS from your terminal.

A data-analysis terminal orchestrated by AI, not a chat client with extra
steps. See the two command groups below: deterministic templates that need
no agent (`analyze`, `market`) ship first; agent-backed commands (`ask`,
`ingest`, `report`) are stubbed until the agent path is verified live.
"""
from __future__ import annotations

import csv
import errno
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from pragmas_sdk import PragmasClient
from pragmas_sdk.analysis import MODULES
from pragmas_sdk.analysis.r_runner import r_available
from pragmas_sdk.exceptions import (
    PragmasAPIError,
    PragmasAuthError,
    PragmasConnectionError,
    PragmasNotImplementedError,
)

from pragmas_cli import __version__
from pragmas_cli.config import config_dir, get_base_url, get_beta_key, save_config

app = typer.Typer(
    name="pragmas",
    help="PRAGMAS from your terminal — analysis templates, public market search, and (soon) the agent.",
)
console = Console()
err_console = Console(stderr=True)

FEEDBACK_URL = "https://github.com/pragmasg/pragmas-cli/issues"

# Pure 7-bit ASCII on purpose — a hand-rolled Unicode banner mangles on
# legacy Windows consoles (the same codepage issue that bit box-drawing
# characters and em-dashes elsewhere in this CLI). Generated with pyfiglet
# ("standard" font), not hand-drawn, so alignment is guaranteed correct.
BANNER = r""" ____  ____      _    ____ __  __    _    ____
|  _ \|  _ \    / \  / ___|  \/  |  / \  / ___|
| |_) | |_) |  / _ \| |  _| |\/| | / _ \ \___ \
|  __/|  _ <  / ___ \ |_| | |  | |/ ___ \ ___) |
|_|   |_| \_\/_/   \_\____|_|  |_/_/   \_\____/"""


def _show_welcome() -> None:
    """Shown when `pragmas` runs with no subcommand. Static on purpose —
    everything here is information you'd otherwise have to dig for
    (what actually works today, whether R is installed, where config
    lives), available instantly with no agent, no network, no login."""
    console.print(f"[cyan]{BANNER}[/cyan]")
    console.print(
        "[bold]Operational intelligence, from your terminal.[/bold] "
        "Local-first, open source — nothing below needs an account.\n"
    )

    quickstart = Table.grid(padding=(0, 2))
    quickstart.add_column(style="cyan", no_wrap=True)
    quickstart.add_column()
    quickstart.add_row(
        'pragmas analyze <file.csv> --template cash_flow_13w', "run a financial template, locally"
    )
    quickstart.add_row('pragmas market "<topic>"', "search public info, no account needed")
    quickstart.add_row("pragmas feedback --open", "tell us what command you want next")
    console.print(Panel(quickstart, title="Quick start", border_style="cyan", expand=False))

    r_ok = r_available()
    env = Table.grid(padding=(0, 2))
    env.add_column(style="dim", no_wrap=True)
    env.add_column()
    env.add_row(
        "Rscript (r:* templates)",
        "[green]found[/green]" if r_ok
        else "[yellow]not found[/yellow] — install R to use r:seasonality/r:outliers/r:correlations",
    )
    env.add_row("Config dir", str(config_dir()))
    env.add_row("Version", __version__)
    console.print(Panel(env, title="Environment", border_style="dim", expand=False))

    console.print("Run [bold]pragmas --help[/bold] for the full command list.\n")


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"pragmas-cli {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: Optional[bool] = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    if ctx.invoked_subcommand is None:
        _show_welcome()


def _client(require_key: bool = False) -> PragmasClient:
    key = get_beta_key()
    if require_key and not key:
        err_console.print(
            Panel(
                "No beta key found. Run [bold]pragmas login[/bold] first — it's free, "
                "no plan or billing involved.",
                title="Not logged in",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=1)
    return PragmasClient(base_url=get_base_url(), beta_key=key)


def _handle_sdk_errors(exc: Exception) -> None:
    if isinstance(exc, PragmasAuthError):
        err_console.print(Panel(str(exc), title="Auth error", border_style="red"))
    elif isinstance(exc, PragmasConnectionError):
        err_console.print(
            Panel(
                f"{exc}\n\nIs the PRAGMAS API up? Check https://pragmas.io or try again shortly.",
                title="Connection error",
                border_style="red",
            )
        )
    elif isinstance(exc, PragmasNotImplementedError):
        err_console.print(
            Panel(
                f"{exc}\n\nWant it sooner? Tell us: [bold]pragmas feedback[/bold]",
                title="Not available yet",
                border_style="yellow",
            )
        )
    elif isinstance(exc, PragmasAPIError):
        err_console.print(Panel(str(exc), title="API error", border_style="red"))
    else:
        err_console.print(Panel(str(exc), title="Unexpected error", border_style="red"))
    raise typer.Exit(code=1)


def _format_table_value(value: object) -> str:
    """Nested lists/dicts (e.g. analyze's per-week breakdown) dump as an
    unreadable wall of text in a table cell — summarize instead and point
    at --output json for the real thing."""
    if isinstance(value, list):
        return f"[{len(value)} items — use --output json for detail]"
    if isinstance(value, dict):
        return f"{{{len(value)} keys — use --output json for detail}}"
    return str(value)


# ── login ──────────────────────────────────────────────────────────────


@app.command()
def login(
    email: str = typer.Option(..., "--email", prompt="Email", help="Email to receive your free beta key."),
    base_url: str = typer.Option(get_base_url(), "--base-url", help="PRAGMAS API URL (advanced)."),
) -> None:
    """Get a free beta key and store it locally (~/.pragmas/credentials.json).

    No plan, no billing — this is the technical-feedback beta. See
    pragmas-sdk's CONTRACT.md for the exact endpoint this calls.
    """
    client = PragmasClient(base_url=base_url)
    try:
        result = client.request_beta_key(email)
    except Exception as exc:  # noqa: BLE001 — routed through _handle_sdk_errors
        _handle_sdk_errors(exc)
        return
    finally:
        client.close()

    save_config(beta_key=result.beta_key, email=result.email, base_url=base_url)
    console.print(Panel(f"Logged in as [bold]{result.email}[/bold]. Beta key saved.", border_style="green"))


# ── analyze ────────────────────────────────────────────────────────────


@app.command()
def analyze(
    input_csv: Path = typer.Argument(
        ..., exists=True, readable=True, help="Path to a local CSV file to analyze."
    ),
    template: str = typer.Option(
        ...,
        "--template",
        help="cash_flow_13w | saas_metrics | ecommerce_unit_economics | r:seasonality | r:outliers | r:correlations",
    ),
    output: str = typer.Option("table", "--output", help="table | json | csv"),
    output_dir: Optional[Path] = typer.Option(
        None, "--output-dir", help="Where to write results.json and any charts (default: a fresh temp dir)."
    ),
) -> None:
    """Run a deterministic analysis template against a local CSV. No agent, no LLM cost, no network.

    Runs entirely on your machine — no login needed, your data never leaves
    this computer. Scriptable by design — pipe --output json/csv straight
    into another tool.
    """
    client = _client(require_key=False)
    try:
        result = client.analyze(
            str(input_csv), template, output_dir=str(output_dir) if output_dir else None
        )
    except Exception as exc:  # noqa: BLE001
        _handle_sdk_errors(exc)
        return
    finally:
        client.close()

    if not result.success:
        err_console.print(Panel(result.error or "Unknown error", title="Analysis failed", border_style="red"))
        raise typer.Exit(code=1)

    if output == "json":
        console.print_json(data=result.model_dump())
    elif output == "csv":
        print("key,value")
        for k, v in result.results.items():
            print(f"{k},{v}")
    else:
        table = Table(title=f"{result.module} — {input_csv.name}")
        table.add_column("Metric")
        table.add_column("Value")
        for k, v in result.results.items():
            table.add_row(str(k), _format_table_value(v))
        console.print(table)
        if result.charts:
            console.print(f"[dim]Charts written: {', '.join(result.charts)}[/dim]")


# ── market ─────────────────────────────────────────────────────────────


@app.command()
def market(
    topic: str = typer.Argument(..., help="Topic to search public/macro information on."),
    max_results: int = typer.Option(5, "--max-results", min=1, max=10),
    output: str = typer.Option("table", "--output", help="table | json | md"),
) -> None:
    """Search public information on a topic. No login required, no tenant data touched.

    The safest command in this CLI to demo in public — see CONTRACT.md.
    """
    client = _client(require_key=False)
    try:
        result = client.market(topic, max_results=max_results)
    except Exception as exc:  # noqa: BLE001
        _handle_sdk_errors(exc)
        return
    finally:
        client.close()

    if output == "json":
        console.print_json(data=result.model_dump())
    elif output == "md":
        print(f"## {result.topic}\n\n{result.summary}\n")
        for s in result.sources:
            print(f"- [{s.title}]({s.url}) — {s.snippet}")
    else:
        console.print(Panel(result.summary, title=result.topic, border_style="cyan"))
        if result.sources:
            table = Table()
            table.add_column("Source")
            table.add_column("URL", overflow="fold")
            for s in result.sources:
                table.add_row(s.title, s.url)
            console.print(table)


# ── inspect ────────────────────────────────────────────────────────────

# Best-effort, sampled — not authoritative. Formats checked for "date-like",
# in order: ISO date, ISO year-month, US slash date. Anything not matching
# one of these three falls through to numeric/categorical instead of being
# reported as a date.
_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m", "%m/%d/%Y")

_INSPECT_SAMPLE_SIZE = 100
_TYPE_MATCH_THRESHOLD = 0.8  # share of sampled non-empty values that must agree
_ID_LIKE_UNIQUE_RATIO = 0.9  # share of sampled non-empty values that must be distinct
_ID_LIKE_MIN_SAMPLE = 5  # don't flag id-like off of a handful of values


def _looks_like_date(value: str) -> bool:
    for fmt in _DATE_FORMATS:
        try:
            datetime.strptime(value, fmt)
            return True
        except ValueError:
            continue
    return False


def _looks_like_numeric(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def _scan_csv(path: Path, sample_size: int = _INSPECT_SAMPLE_SIZE) -> tuple[list[str], int, list[list[str]]]:
    """One streaming pass: exact row count (all rows), but only the first
    `sample_size` data rows are kept in memory for type detection — a
    multi-GB CSV is never fully materialized."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        sample_rows: list[list[str]] = []
        row_count = 0
        for row in reader:
            row_count += 1
            if len(sample_rows) < sample_size:
                sample_rows.append(row)
    return header, row_count, sample_rows


def _detect_column_types(header: list[str], sample_rows: list[list[str]]) -> "dict[str, str]":
    """Best-effort type per column, over the sampled rows only. Returns a
    human-readable label, never raises on ragged/short rows."""
    labels: dict[str, str] = {}
    for idx, col in enumerate(header):
        values = [row[idx].strip() for row in sample_rows if idx < len(row) and row[idx].strip() != ""]
        non_empty = len(values)
        if non_empty == 0:
            labels[col] = "unknown (no data in sample)"
            continue

        date_ratio = sum(1 for v in values if _looks_like_date(v)) / non_empty
        numeric_ratio = sum(1 for v in values if _looks_like_numeric(v)) / non_empty
        unique_ratio = len(set(values)) / non_empty
        id_like = non_empty >= _ID_LIKE_MIN_SAMPLE and unique_ratio > _ID_LIKE_UNIQUE_RATIO

        if date_ratio >= _TYPE_MATCH_THRESHOLD:
            labels[col] = "date-like"
        elif id_like:
            labels[col] = "id-like"
        elif numeric_ratio >= _TYPE_MATCH_THRESHOLD:
            labels[col] = "numeric"
        else:
            labels[col] = "categorical"
    return labels


def _human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024 or unit == "TB":
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"  # pragma: no cover — unreachable, kept for clarity


def _potential_templates(header: list[str]) -> list[str]:
    """Templates whose REQUIRED_COLS is an exact-name subset of this CSV's
    header. A template with REQUIRED_COLS == [] (e.g. a universal profiling
    template, if present) matches everything. r:* templates aren't in
    MODULES and have no REQUIRED_COLS to check, so they're skipped rather
    than guessed at."""
    header_set = set(header)
    matches = []
    for name, fn in sorted(MODULES.items()):
        required = getattr(sys.modules[fn.__module__], "REQUIRED_COLS", None)
        if required is None:
            continue
        if len(required) == 0 or set(required).issubset(header_set):
            matches.append(name)
    return matches


@app.command()
def inspect(
    input_csv: Path = typer.Argument(..., exists=True, readable=True, help="Path to a local CSV file."),
) -> None:
    """Inspect a CSV and suggest which analysis templates it might fit.

    Row/column counts and file size are exact (one streaming pass, nothing
    fully loaded into memory). Per-column type detection ("date-like",
    "numeric", "categorical", "id-like") is best-effort and sampled from
    only the first 100 data rows — treat it as a hint, not ground truth,
    especially on a CSV with mixed or dirty data further down. Potential
    templates are found by comparing your CSV's header (exact, case-sensitive
    name match — no fuzzy matching) against each template's required columns.
    """
    header, row_count, sample_rows = _scan_csv(input_csv)
    col_types = _detect_column_types(header, sample_rows)
    matches = _potential_templates(header)

    dataset = Table.grid(padding=(0, 2))
    dataset.add_column(style="dim", no_wrap=True)
    dataset.add_column()
    dataset.add_row("Rows", f"{row_count:,}")
    dataset.add_row("Columns", str(len(header)))
    dataset.add_row("Size", _human_size(input_csv.stat().st_size))
    console.print(Panel(dataset, title="Dataset", border_style="cyan", expand=False))

    detected = Table.grid(padding=(0, 2))
    detected.add_column(style="cyan", no_wrap=True)
    detected.add_column()
    if header:
        for col in header:
            detected.add_row(col, f"[green]OK[/green] ({col_types.get(col, 'unknown')})")
    else:
        detected.add_row("[dim]No columns found (empty file?)[/dim]", "")
    console.print(Panel(detected, title="Detected", border_style="cyan", expand=False))

    templates = Table.grid(padding=(0, 2))
    templates.add_column()
    if matches:
        for name in matches:
            templates.add_row(f"[green]OK[/green] {name}")
    else:
        templates.add_row("[dim]No potential template matches for these columns.[/dim]")
    console.print(Panel(templates, title="Potential templates", border_style="cyan", expand=False))

    console.print(
        f"[dim]Row/column counts and size are exact. Type detection is sampled from the "
        f"first {min(row_count, _INSPECT_SAMPLE_SIZE)} of {row_count:,} data row(s) and is "
        f"best-effort, not authoritative.[/dim]"
    )


# ── feedback ───────────────────────────────────────────────────────────


@app.command()
def feedback(
    open_browser: bool = typer.Option(False, "--open", help="Open the issue tracker in your browser."),
) -> None:
    """Tell us what command or feature you want next.

    This CLI exists to collect technical feedback, not to sell a plan —
    this is the loop that closes that.
    """
    console.print(f"Open an issue: [bold underline]{FEEDBACK_URL}[/bold underline]")
    if open_browser:
        import webbrowser

        webbrowser.open(FEEDBACK_URL)


# ── v0.2 — agent-backed, stubbed until verified live in production ─────

report_app = typer.Typer(help="Report generation (agent-backed — coming in v0.2).")
app.add_typer(report_app, name="report")


@app.command()
def ask(query: str = typer.Argument(..., help="Ask the PRAGMAS agent a question about your data.")) -> None:
    """[v0.2] Ask the agent. Not available yet — see `pragmas feedback`."""
    client = _client(require_key=True)
    try:
        client.ask(query)
    except Exception as exc:  # noqa: BLE001
        _handle_sdk_errors(exc)
    finally:
        client.close()


@app.command()
def ingest(file: str = typer.Argument(..., help="File to ingest into a project.")) -> None:
    """[v0.2] Ingest a document. Not available yet — see `pragmas feedback`."""
    client = _client(require_key=True)
    try:
        client.ingest(file)
    except Exception as exc:  # noqa: BLE001
        _handle_sdk_errors(exc)
    finally:
        client.close()


@report_app.command("generate")
def report_generate(
    project: str = typer.Option(..., "--project"),
    type: str = typer.Option("financial", "--type"),
) -> None:
    """[v0.2] Generate a PDF/PPTX report. Not available yet — see `pragmas feedback`."""
    client = _client(require_key=True)
    try:
        client.generate_report(project=project, type=type)
    except Exception as exc:  # noqa: BLE001
        _handle_sdk_errors(exc)
    finally:
        client.close()


@app.command()
def tui() -> None:
    """[v0.2] Interactive terminal dashboard. Not available yet — see `pragmas feedback`."""
    err_console.print(
        Panel(
            f"The interactive dashboard is planned for v0.2. Want it sooner? "
            f"[bold]pragmas feedback --open[/bold]\n\n{FEEDBACK_URL}",
            title="Not available yet",
            border_style="yellow",
        )
    )
    raise typer.Exit(code=1)


def main() -> None:
    """Entry point (also what the installed `pragmas` script calls).

    Swallows a broken-pipe condition instead of a raw traceback — piping
    into something that truncates early (`pragmas analyze x.csv --output
    json | head`) closes the read end before we're done writing, which is
    the reader's choice, not a real failure. POSIX raises `BrokenPipeError`
    for this; legacy Windows consoles raise a plain `OSError` with
    `errno == EINVAL` instead, so both are handled here.
    """
    try:
        app()
    except BrokenPipeError:
        sys.exit(0)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.EPIPE):
            sys.exit(0)
        raise


if __name__ == "__main__":
    main()
