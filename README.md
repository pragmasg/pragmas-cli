# pragmas-cli

**[PRAGMAS](https://pragmas.io) from your terminal** — deterministic
financial analysis templates and public market search, scriptable, built on
[`pragmas-sdk`](https://github.com/pragmasg/pragmas-sdk). A data-analysis
terminal orchestrated by AI, not a chat client with extra steps.

[![PyPI version](https://img.shields.io/pypi/v/pragmas-cli.svg)](https://pypi.org/project/pragmas-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/pragmas-cli.svg)](https://pypi.org/project/pragmas-cli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

The PRAGMAS backend is closed source. This CLI is MIT and open on purpose:
it exists to collect technical feedback on command design from early
adopters, not to sell a plan. See
[`pragmas-sdk`](https://github.com/pragmasg/pragmas-sdk)'s
[`CONTRACT.md`](https://github.com/pragmasg/pragmas-sdk/blob/main/CONTRACT.md)
for exactly which backend endpoints are live in production today versus
planned — every command below is honest about which bucket it's in.

## Install

```bash
pip install pragmas-cli
```

Requires Python 3.9+. Installing `pragmas-cli` pulls in `pragmas-sdk`
automatically.

## Quickstart

`--version` and `feedback` work today, no backend involved:

```console
$ pragmas --version
pragmas-cli 0.1.0

$ pragmas feedback --open
Open an issue: https://github.com/pragmasg/pragmas-cli/issues
```

`login`, `analyze`, and `market` target the beta-key and analysis endpoints
that are shipping next (🟡, see [Status](#status)). Run them today and you
get a clear, styled error instead of a hang or a stack trace — this is real
output from the CLI right now:

```console
$ pragmas analyze acme --template cash_flow_13w
╭─ Not logged in ──────────────────────────────────────────────────╮
│ No beta key found. Run pragmas login first — it's free, no plan  │
│ or billing involved.                                             │
╰────────────────────────────────────────────────────────────────────╯
```

Once those endpoints are live, the same commands look like this — exact
formatting, pulled straight from the CLI's (already tested) output code:

```console
$ pragmas login --email you@example.com
Logged in as you@example.com. Beta key saved.

$ pragmas analyze acme --template cash_flow_13w
        cash_flow_13w — acme
┏━━━━━━━━┳━━━━━━━┓
┃ Metric ┃ Value ┃
┡━━━━━━━━╇━━━━━━━┩
│ weeks  │ 13    │
└────────┴───────┘

$ pragmas market "interest rates real estate LATAM" --output md
## real estate LATAM

Rates trending down.

- [Reuters](https://example.com) — ...
```

## Commands

| Command | Backend endpoint | Status |
|---|---|---|
| `pragmas login --email you@example.com` | `POST /auth/beta-key` | 🟡 planned — free beta key, no plan/billing |
| `pragmas analyze <project> --template <name> --output table\|json\|csv` | `POST /projects/{id}/analyze` | 🟡 planned — deterministic, no LLM cost |
| `pragmas market "<topic>" --max-results N --output table\|json\|md` | `GET /market` | 🟡 planned — no login required |
| `pragmas feedback [--open]` | GitHub issues, no backend | 🟢 works today |
| `pragmas ask <query>` | agent (streaming) | ⚪ v0.2 — prints "not available yet" |
| `pragmas ingest <file>` | document upload | ⚪ v0.2 — prints "not available yet" |
| `pragmas report generate --project <id> --type <type>` | report generation | ⚪ v0.2 — prints "not available yet" |
| `pragmas tui` | interactive dashboard | ⚪ v0.2 — prints "not available yet" |

🟢 live · 🟡 endpoint planned, backend-side — command exists and is tested,
calling it against production today raises a friendly connection/auth error
· ⚪ intentionally stubbed, no backend contract targeted yet.

`analyze --template` accepts `cash_flow_13w`, `saas_metrics`,
`ecommerce_unit_economics`, `r:seasonality`, `r:outliers`, or
`r:correlations`.

### Why this order

`analyze` and `market` don't touch the LLM agent — they're deterministic and
ready to ship the moment their endpoints go live, independent of the
agent/RAG path. `ask`, `ingest`, `report generate`, and `tui` exist as real
commands today and fail loudly and clearly rather than pretending to work,
pointing you at `pragmas feedback` instead.

### Configuration

Credentials from `pragmas login` are stored in `~/.pragmas/credentials.json`.
Override with environment variables when you need to:

| Variable | Purpose |
|---|---|
| `PRAGMAS_BASE_URL` | API root (defaults to `https://api.pragmas.io`) |
| `PRAGMAS_BETA_KEY` | Skip the credentials file, e.g. in CI |
| `PRAGMAS_CONFIG_DIR` | Override `~/.pragmas` |

## Give feedback

This CLI exists to collect feedback on command design before the platform
goes GA — that's what `pragmas feedback` is for. Run it, or
[open an issue](https://github.com/pragmasg/pragmas-cli/issues) directly.

## What's next

`ask`, `ingest`, `report generate`, and `tui` become real once the beta-key
flow and deterministic-analysis endpoint above are live and the agent/RAG
path has been verified end-to-end in production. Until then they raise a
clear "not available yet" error rather than shipping half-working.

## Development

```bash
# from a checkout of both repos as siblings
git clone https://github.com/pragmasg/pragmas-sdk.git
git clone https://github.com/pragmasg/pragmas-cli.git

cd pragmas-sdk && pip install -e .
cd ../pragmas-cli && pip install -e ".[dev]"
pytest
```

Tests drive the CLI with Typer's `CliRunner` and mock the HTTP layer with
[`respx`](https://lundberg.github.io/respx/) — no live backend required.

## License

MIT — see [LICENSE](./LICENSE).
