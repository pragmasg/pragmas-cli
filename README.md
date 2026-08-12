# pragmas-cli

PRAGMAS from your terminal — a data-analysis terminal orchestrated by AI, not
a chat client with extra steps.

MIT-licensed and open source on purpose: this is a beta-feedback tool, not a
monetized product surface. See [pragmas-sdk's
CONTRACT.md](https://github.com/pragmasg/pragmas-sdk/blob/main/CONTRACT.md)
for exactly which endpoints are live in production today versus planned.

## Install

```bash
pip install pragmas-cli
```

(Not published to PyPI yet — pre-release. Install from a local checkout, see
Development below.)

## Commands

### v0.1 — deterministic, no agent, no LLM cost

```bash
pragmas login                                             # free beta key, no plan/billing
pragmas analyze acme --template cash_flow_13w --output json
pragmas analyze acme --template saas_metrics --output csv
pragmas market "interest rates real estate LATAM" --output md   # no login needed
pragmas feedback --open                                   # tell us what's missing
```

### v0.2 — agent-backed, ships once the agent path is verified live

`ask`, `ingest`, `report generate`, `tui` exist as commands today but raise a
clear "not available yet" message rather than pretending to work — run any of
them to see the pointer to `pragmas feedback`.

## Why this order

`analyze` and `market` don't touch the LLM agent at all — they're
deterministic and safe to ship even while the agent/RAG path is still being
verified in production. See the PRAGMAS GTM plan for the reasoning.

## Development

```bash
# from a checkout of both repos as siblings:
cd ../pragmas-sdk && pip install -e .
cd ../pragmas-cli && pip install -e ".[dev]"
pytest
```

## License

MIT — see [LICENSE](./LICENSE).
