"""pragmas — PRAGMAS from your terminal.

A data-analysis terminal orchestrated by AI, not a chat client with extra
steps. See the two command groups below: deterministic templates that need
no agent (`analyze`, `market`) ship first; agent-backed commands (`ask`,
`ingest`, `report`) are stubbed until the agent path is verified live.
"""
from __future__ import annotations

import json
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from pragmas_sdk import PragmasClient
from pragmas_sdk.exceptions import (
    PragmasAPIError,
    PragmasAuthError,
    PragmasConnectionError,
    PragmasNotImplementedError,
)

from pragmas_cli import __version__
from pragmas_cli.config import get_base_url, get_beta_key, save_config

app = typer.Typer(
    name="pragmas",
    help="PRAGMAS from your terminal — analysis templates, public market search, and (soon) the agent.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)

FEEDBACK_URL = "https://github.com/pragmasg/pragmas-cli/issues"


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"pragmas-cli {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Optional[bool] = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    pass


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
    project: str = typer.Argument(..., help="Project ID to analyze."),
    template: str = typer.Option(
        ...,
        "--template",
        help="cash_flow_13w | saas_metrics | ecommerce_unit_economics | r:seasonality | r:outliers | r:correlations",
    ),
    output: str = typer.Option("table", "--output", help="table | json | csv"),
) -> None:
    """Run a deterministic analysis template against a project. No agent, no LLM cost.

    Scriptable by design — pipe --output json/csv straight into another tool.
    """
    client = _client(require_key=True)
    try:
        result = client.analyze(project, template)
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
        table = Table(title=f"{result.module} — {project}")
        table.add_column("Metric")
        table.add_column("Value")
        for k, v in result.results.items():
            table.add_row(str(k), str(v))
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


if __name__ == "__main__":
    app()
