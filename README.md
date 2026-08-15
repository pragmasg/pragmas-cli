# PRAGMAS

**Financial analysis and market research, from your terminal — no account, no
setup, your data never leaves your machine.**

[![PyPI version](https://img.shields.io/pypi/v/pragmas-cli.svg)](https://pypi.org/project/pragmas-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/pragmas-cli.svg)](https://pypi.org/project/pragmas-cli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## What is PRAGMAS?

[PRAGMAS](https://pragmas.io) is about giving anyone the same rigor a
financial analyst would apply — cash flow projections, SaaS metrics, unit
economics, cohort retention — without building a dashboard, learning a BI
tool, or writing SQL. Point it at your data, get the analysis back.

`pragmas-cli` is the free, open-source terminal client for that toolkit: a
growing library of financial-analysis templates and a public-research tool
that run entirely on your computer. No account, no API key, no data ever
sent anywhere — install it and use it, full stop.

## What does this CLI actually do, today?

Two things, both real, both running on your machine:

1. **`pragmas analyze`** — feed it a CSV, get back a structured financial
   analysis: a 13-week cash flow projection, SaaS metrics (MRR bridge,
   churn, Rule of 40), e-commerce unit economics, or statistical templates
   (seasonality, outlier detection, correlation matrices) written in R. The
   math is the same math an analyst would do in a spreadsheet — this just
   does it in one command and hands you the numbers and a chart.
2. **`pragmas market`** — search public information on a topic (news,
   benchmarks, industry data) and get a short summary with sources. No
   account needed — it's a plain web search, nothing about your business
   is involved.

Everything else (`ask`, `ingest`, `report generate`, a live dashboard) is
what the rest of PRAGMAS does — those commands exist here as placeholders
so you can see the intended shape, and they tell you plainly that they're
not built into this CLI yet rather than pretending to work.

## Install

```bash
pip install pragmas-cli
```

Requires Python 3.9+. Installing `pragmas-cli` pulls in `pragmas-sdk`
automatically.

### Standalone binaries (no Python needed)

Each [GitHub Release](https://github.com/pragmasg/pragmas-cli/releases)
ships pre-built single-file binaries for Windows, Linux and macOS (Apple
Silicon) — download and run `pragmas` directly, no Python install required.

- `pragmas-windows-x64.exe`
- `pragmas-linux-x64`
- `pragmas-macos-arm64`

Notes:
- macOS binaries are unsigned — right-click → **Open** the first time to
  bypass Gatekeeper. Intel Macs: use `pip install pragmas-cli` (the binary
  is Apple Silicon only).
- The `r:*` templates still need `Rscript` installed on your machine even
  when using a binary (see below).

## Quickstart

Run `pragmas` with no arguments and you get a welcome screen: a banner, the
commands that work with zero setup, and whether `Rscript` is actually
installed on your machine — checked for real, not guessed.

<img src="docs/screenshots/welcome.png" alt="pragmas welcome screen: ASCII banner, a Quick start panel listing analyze/market/feedback, and an Environment panel showing Rscript status, config dir, and version" width="480">

Point `analyze` at a CSV — no signup, no API key, no internet connection to
anything PRAGMAS-owned:

```console
$ pragmas analyze cashflow.csv --template cash_flow_13w
```

<img src="docs/screenshots/analyze.png" alt="pragmas analyze output: a table of cash flow metrics (start date, opening balance, min balance, weeks negative, total inflows/outflows, net) plus a line confirming a chart was written to disk" width="440">

`market` needs even less — not even a beta key:

```console
$ pragmas market "SaaS churn benchmarks" --max-results 3
```

<img src="docs/screenshots/market.png" alt="pragmas market output: a summary panel titled 'SaaS churn benchmarks' followed by a table of three sources with their URLs" width="480">

Bad input doesn't crash — it tells you what's wrong and what your options
are:

```console
$ pragmas analyze cashflow.csv --template not_real
```

<img src="docs/screenshots/error-handling.png" alt="pragmas error output: a red 'Analysis failed' panel listing the unknown template name and the full list of valid templates" width="480">

`login` is only relevant for the agent-backed commands further down (`ask`,
`ingest`, `report generate`) — `analyze` and `market` never need it.

## Commands

| Command | Runs | Status |
|---|---|---|
| `pragmas analyze <csv> --template <name> --output table\|json\|csv [--param k=v ...]` | locally | 🟢 works today, no network |
| `pragmas templates` / `pragmas templates show <name>` | locally | 🟢 works today, no network |
| `pragmas validate <csv> --template <name>` | locally | 🟢 works today, no network |
| `pragmas inspect <csv>` | locally | 🟢 works today, no network |
| `pragmas doctor [--check-api]` | locally (offline unless `--check-api`) | 🟢 works today |
| `pragmas market "<topic>" --max-results N --output table\|json\|md` | locally | 🟢 works today, no network |
| `pragmas feedback [--open]` | GitHub issues | 🟢 works today |
| `pragmas login --email you@example.com` | `POST /auth/beta-key` | 🟡 implemented backend-side, not deployed yet |
| `pragmas` (no args) / `pragmas tui` | interactive session, locally | 🟢 works today, no network, no login |
| `pragmas ask <query>` | agent (streaming) | ⚪ v0.2 — prints "not available yet" |
| `pragmas ingest <file>` | document upload | ⚪ v0.2 — prints "not available yet" |
| `pragmas report generate --project <id> --type <type>` | report generation | ⚪ v0.2 — prints "not available yet" |

🟢 works today, fully local · 🟡 needs a backend running at `--base-url` (not
publicly hosted yet — works against one you run yourself) · ⚪ intentionally
stubbed, not implemented yet.

**`pragmas` is now the standard way to run this CLI**: with no arguments (in
a real terminal) it drops you into a real [Textual](https://textual.textualize.io/)
app — persistent sidebar (current model/tools, quick commands, environment
status), an independently-scrollable chat log, a bottom prompt with command
history (↑/↓) and `/command` Tab-completion. Resizes cleanly; the sidebar
auto-collapses under 80 columns and disappears (with a small toggle to bring
it back, or `Ctrl+B`) under 40. Piped/non-interactive input (no real tty —
CI, `pragmas < /dev/null`) still gets the old static welcome screen instead,
same as always; a Textual app can't run without a real terminal any more
than a REPL loop could read input that would never arrive. Everything in the
🟢 rows above is reachable as a `/slash` command (`/analyze`, `/validate`,
`/inspect`, `/templates`, `/market`, `/doctor`, `/login`, `/model`,
`/feedback`) — type `/help` inside it for the full list, or click one in the
sidebar. Every command also still works exactly as before as a direct,
scriptable one-shot invocation (`pragmas analyze cashflow.csv --template
cash_flow_13w --output json | jq ...`) — the TUI is an added front door, not
a replacement for scripting. Free text that isn't a `/command` depends on
whether a local Ollama server is running — see
[Local agent mode](#local-agent-mode-ollama) below — but never pretends to
be a chat agent when there isn't one available. `ask`/`ingest`/`report
generate` (the *PRAGMAS backend* agent, a separate thing entirely — see
below) are unaffected by any of this: still stubbed, and not reachable from
inside the TUI, pending a backend-side design decision (mapping a beta key
to a tenant) that hasn't been made yet.

`analyze --template` accepts any name from `pragmas templates` — run it for
the current, always-accurate list (new templates in the SDK show up here
automatically, nothing to update in this CLI). As of this writing:
`cash_flow_13w`, `saas_metrics`, `ecommerce_unit_economics`, `data_profile`,
`sales_pipeline`, `burn_rate_runway`, `cohort_analysis`, `board_report`,
`r:seasonality`, `r:outliers`, `r:correlations`. The `r:*` templates need
`Rscript` installed **on your own machine** — everything else needs nothing
beyond `pip install`. Unknown templates, missing files, or missing `Rscript`
all produce a clear message and a non-zero exit code, never a crash — see
the error example above. `pragmas validate <csv> --template <name>` checks
your columns match before you run anything; `pragmas inspect <csv>`
suggests which templates a CSV might fit without knowing one up front.

### Why analyze/market are local, not "coming soon on the server"

They're deterministic — no LLM, no proprietary model — so there's no reason
to route them through a server: it would only add latency, a network
dependency, and cost for no benefit, and a public zero-auth endpoint like
`market` is real abuse surface for a hosted service with nothing to gain by
hosting it. `ask`, `ingest`, and `report generate` are different: they
genuinely need the agent, RAG, and document storage, which can't run
meaningfully offline. Those exist as real commands today and fail loudly and
clearly rather than pretending to work, pointing you at `pragmas feedback`
instead. `tui` isn't in that group — the interactive session itself needs no
agent at all, only the same local commands above; it's real today, and its
free-text handling is explicit that it isn't a chat agent rather than faking
one.

### Local agent mode (Ollama)

If a [local Ollama](https://ollama.com) server is running when `pragmas`
starts, the TUI upgrades itself: free text becomes a real chat turn with
whichever model it picked (prefers one Ollama itself reports as
`"tools"`-capable), streamed live. With a tools-capable model, the model can
call `analyze`/`inspect`/`validate`/`templates`/`market` *itself* —
"what template fits this csv" really runs `/inspect`, reads the real
columns, and answers from that, not from a guess. No PRAGMAS account, no
data leaves your machine — this talks straight to your local Ollama, it has
nothing to do with the PRAGMAS backend or `ask`/`ingest`/`report generate`
above.

No Ollama running (or no chat-capable model pulled) → the TUI falls back to
exactly the local-command-only behavior described above ("modo
programación"), same as it worked before this existed — never a hang, never
a fake chat reply.

`/model` inside the TUI shows every detected model and lets you switch
(`/model llama3.2:1b`, starts a fresh conversation) — useful since not every
model is equally reliable at actually invoking a tool rather than just
describing one in plain text
(small/quantized local models can be inconsistent about this; a
tools-capable model still won't call a tool 100% of the time, that's a
model-quality thing, not something this CLI controls).

### Configuration

Credentials from `pragmas login` are stored in `~/.pragmas/credentials.json`.
Override with environment variables when you need to:

| Variable | Purpose |
|---|---|
| `PRAGMAS_BASE_URL` | API root for `login`/future agent commands (defaults to `https://api.pragmas.io`) |
| `PRAGMAS_BETA_KEY` | Skip the credentials file, e.g. in CI |
| `PRAGMAS_CONFIG_DIR` | Override `~/.pragmas` |
| `PRAGMAS_OLLAMA_URL` | Where to reach Ollama for local agent mode (defaults to `http://127.0.0.1:11434`; also honors Ollama's own `OLLAMA_HOST`, rewriting a `0.0.0.0`/`[::]` bind-all host to `127.0.0.1` since that's a server address, not a valid client target) |

## Give feedback

Tell us what's awkward, missing, or surprising about a command — that's what
`pragmas feedback` is for. Run it, or
[open an issue](https://github.com/pragmasg/pragmas-cli/issues) directly.

## What's next

This CLI's own roadmap tracks the SDK's: new analysis templates and named
data connectors show up here automatically as they land in `pragmas-sdk`
(see [Commands](#commands) above — `analyze --template` always reflects the
current list, nothing to update in this repo for that). `analyze`/`market`
are staying local for good — see
[`pragmas-sdk`'s CONTRACT.md](https://github.com/pragmasg/pragmas-sdk/blob/main/CONTRACT.md)
for why. `ask`, `ingest`, and `report generate` are reserved for
agent-backed capabilities this CLI doesn't implement yet.

## Development

```bash
# from a checkout of both repos as siblings
git clone https://github.com/pragmasg/pragmas-sdk.git
git clone https://github.com/pragmasg/pragmas-cli.git

cd pragmas-sdk && pip install -e .
cd ../pragmas-cli && pip install -e ".[dev]"
pytest
```

`analyze`/`market` tests run for real (pandas/matplotlib, a fake DuckDuckGo
backend) via Typer's `CliRunner`; `login` mocks the HTTP layer with
[`respx`](https://lundberg.github.io/respx/) — no live backend required
either way.

## Contributing

New commands, flags, output formatting, and error-handling improvements are
welcome. Most new *capabilities* (analysis templates, connectors) belong in
[`pragmas-sdk`](https://github.com/pragmasg/pragmas-sdk) instead — this repo
should consume the SDK, not duplicate its logic. See
[CONTRIBUTING.md](./CONTRIBUTING.md) for the full guide: what's open today,
dev setup, and the PR process.

## License

MIT — see [LICENSE](./LICENSE).
