# pragmas-cli

**[PRAGMAS](https://pragmas.io) from your terminal** — deterministic
financial analysis and public market search, built on
[`pragmas-sdk`](https://github.com/pragmasg/pragmas-sdk). Runs entirely on
your machine: no account, no API key, no network call to any PRAGMAS server,
your data never leaves your computer. A data-analysis terminal, not a chat
client with extra steps.

[![PyPI version](https://img.shields.io/pypi/v/pragmas-cli.svg)](https://pypi.org/project/pragmas-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/pragmas-cli.svg)](https://pypi.org/project/pragmas-cli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

The PRAGMAS backend is closed source. This CLI is MIT and open on purpose:
it exists to collect technical feedback on command design from early
adopters, not to sell a plan. See
[`pragmas-sdk`](https://github.com/pragmasg/pragmas-sdk)'s
[`CONTRACT.md`](https://github.com/pragmasg/pragmas-sdk/blob/main/CONTRACT.md)
for exactly what's local versus what still needs the (hosted, closed-source)
backend.

## Install

```bash
pip install pragmas-cli
```

Requires Python 3.9+. Installing `pragmas-cli` pulls in `pragmas-sdk`
automatically.

## Quickstart

No signup, no API key, no internet connection to anything PRAGMAS-owned —
point it at a CSV:

```console
$ pragmas analyze cashflow.csv --template cash_flow_13w
        cash_flow_13w — cashflow.csv
┏━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ Metric            ┃ Value         ┃
┡━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ min_balance        │ -1000.0       │
│ weeks_negative      │ 1             │
│ total_inflows       │ 15000.0       │
│ total_outflows      │ -12000.0      │
└────────────────────┴───────────────┘
Charts written: /tmp/pragmas_analysis_xyz/cash_flow_13w.png

$ pragmas market "interest rates real estate LATAM" --output md
## interest rates real estate LATAM

Rates trending down.

- [Reuters](https://example.com) — ...
```

`login` is only relevant for the agent-backed commands below it (`ask`,
`ingest`, `report generate`) — `analyze` and `market` never need it.

## Commands

| Command | Runs | Status |
|---|---|---|
| `pragmas analyze <csv> --template <name> --output table\|json\|csv` | locally | 🟢 works today, no network |
| `pragmas market "<topic>" --max-results N --output table\|json\|md` | locally | 🟢 works today, no network |
| `pragmas feedback [--open]` | GitHub issues | 🟢 works today |
| `pragmas login --email you@example.com` | `POST /auth/beta-key` | 🟡 planned |
| `pragmas ask <query>` | agent (streaming) | ⚪ v0.2 — prints "not available yet" |
| `pragmas ingest <file>` | document upload | ⚪ v0.2 — prints "not available yet" |
| `pragmas report generate --project <id> --type <type>` | report generation | ⚪ v0.2 — prints "not available yet" |
| `pragmas tui` | interactive dashboard | ⚪ v0.2 — prints "not available yet" |

🟢 works today · 🟡 targets a documented but not-yet-shipped backend endpoint
· ⚪ intentionally stubbed, no backend contract targeted yet.

`analyze --template` accepts `cash_flow_13w`, `saas_metrics`,
`ecommerce_unit_economics`, `r:seasonality`, `r:outliers`, or
`r:correlations`. The `r:*` templates need `Rscript` installed **on your own
machine** — everything else needs nothing beyond `pip install`. Unknown
templates, missing files, or missing `Rscript` all produce a clear message
and a non-zero exit code, never a crash.

### Why analyze/market are local, not "planned"

They're deterministic — no LLM, no proprietary model — so there's no reason
to route them through a server: it would only add latency, a network
dependency, and cost for no benefit, and a public zero-auth endpoint like
`market` is real abuse surface for a hosted service with nothing to gain by
hosting it. `ask`, `ingest`, `report generate`, and `tui` are different: they
genuinely need the agent, RAG, and document storage, which can't run
meaningfully offline. Those exist as real commands today and fail loudly and
clearly rather than pretending to work, pointing you at `pragmas feedback`
instead.

### Configuration

Credentials from `pragmas login` (once its endpoint ships) are stored in
`~/.pragmas/credentials.json`. Override with environment variables when you
need to:

| Variable | Purpose |
|---|---|
| `PRAGMAS_BASE_URL` | API root for `login`/future agent commands (defaults to `https://api.pragmas.io`) |
| `PRAGMAS_BETA_KEY` | Skip the credentials file, e.g. in CI |
| `PRAGMAS_CONFIG_DIR` | Override `~/.pragmas` |

## Give feedback

This CLI exists to collect feedback on command design before the platform
goes GA — that's what `pragmas feedback` is for. Run it, or
[open an issue](https://github.com/pragmasg/pragmas-cli/issues) directly.

## What's next

`ask`, `ingest`, `report generate`, and `tui` become real once the beta-key
flow is live and the agent/RAG path has been verified end-to-end in
production. `analyze`/`market` are staying local for good — see
[`pragmas-sdk`'s CONTRACT.md](https://github.com/pragmasg/pragmas-sdk/blob/main/CONTRACT.md)
for why.

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

## License

MIT — see [LICENSE](./LICENSE).
