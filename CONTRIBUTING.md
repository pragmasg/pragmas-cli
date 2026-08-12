# Contributing to pragmas-cli

Thanks for considering it. This doc exists so you know what to touch, what
not to touch, and what to expect before you spend time on a PR.

## Where this fits

`pragmas-cli` is the terminal interface for [PRAGMAS](https://pragmas.io),
built directly on top of [`pragmas-sdk`](https://github.com/pragmasg/pragmas-sdk)
— it should **consume** the SDK, not duplicate its logic. If what you're
adding is a new analysis template, a data connector, or any other Python
capability, it belongs in `pragmas-sdk`, not here — open a PR there instead
(see [its CONTRIBUTING.md](https://github.com/pragmasg/pragmas-sdk/blob/master/CONTRIBUTING.md)).
This repo is about the command surface: how that capability is invoked,
formatted, and explained from a terminal.

The product's differential logic — RAG, LangGraph, the agent, orchestration —
lives in a **private** repo (`noname`) that this repo has no access to and
never will. You don't need to know how any of that works to contribute here.

## Where we are right now

We're deliberately **not** trying to open-source the whole product — just
its extension surface, and we're doing that in phases:

- **Now**: `analyze` and `market` are real, fully working commands with no
  login required. `login` works if you point it at a backend you're running
  yourself. `ask`, `ingest`, `report generate`, and `tui` exist as commands
  but print "not available yet" — the backend they'd need isn't live in
  production.
- **Later**: those agent-backed commands become real once the agent/RAG path
  is verified live. `pragmas-sdk`'s `CONTRACT.md` is the source of truth for
  what's actually live versus planned — check it before assuming a command
  can be fully wired up.

## What you can contribute today

| Area | Status | Notes |
|---|---|---|
| New flags/options on an existing command (`analyze`, `market`) | 🟢 open | e.g. exposing template params — see [Known gaps](#known-gaps) below |
| Output formatting (table/json/csv/md rendering) | 🟢 open | |
| Error handling / messages | 🟢 open | Keep the "never a raw traceback" convention — see `main()`'s broken-pipe handling for the pattern |
| Welcome screen / `--help` text / docs | 🟢 open | |
| A genuinely new command | 🟡 open an issue first | Needs to map to a real SDK capability — see below |
| Wiring up `ask`/`ingest`/`report generate`/`tui` for real | ⚪ not yet | Blocked on the backend, not on this repo |

### Adding a flag to an existing command

Follow the pattern in `src/pragmas_cli/main.py`: Typer options with a short
`help=`, sensible defaults, and validation that produces a clear `rich`
panel via `_handle_sdk_errors`/`err_console` rather than a raw exception.
Add a test in `tests/test_main.py` using Typer's `CliRunner`.

### Known gaps (good first contributions)

- `analyze` has no `--param` flag — template params like `opening_balance`
  (cash flow), `cac` (SaaS/e-commerce), `ad_spend_by_channel` always fall
  back to defaults because there's no way to set them from the command line
  even though the underlying SDK templates accept them. A `--param key=value`
  (repeatable) flag wired to `PragmasClient.analyze(..., params=...)` would
  close this without needing any SDK change.
- `tui` is a stub — a real interactive dashboard is planned but not started.

### Proposing a new command

Every command here should map to something `pragmas-sdk` already exposes on
`PragmasClient` (or a documented planned capability, per `CONTRACT.md`). If
the SDK doesn't have the capability yet, that PR belongs in `pragmas-sdk`
first. [Open an issue](https://github.com/pragmasg/pragmas-cli/issues)
describing:

- What the command would do and what SDK method it calls.
- What the output should look like for each `--output` mode you'd support.
- Whether it needs `login`/a beta key (agent-backed commands do; `analyze`/
  `market` don't and shouldn't start needing one).

## What not to contribute here

- Any analysis logic, connector, or other capability that doesn't already
  exist on `PragmasClient` — that's an SDK PR, not a CLI one, even if the
  end goal is a new command. Duplicating logic here instead of adding it to
  the SDK is exactly the pattern this repo is built to avoid.
- Anything that requires knowledge of the private core's internals (RAG
  behavior, LangGraph structure, agent prompts).
- API keys, tokens, or credentials of any kind, in code, tests, or examples.
- Hand-drawn Unicode banners/box-drawing art — the existing banner is plain
  ASCII on purpose (legacy Windows consoles mangle Unicode box-drawing and
  em-dashes); keep any new terminal art in the same safe subset.

## Development setup

```bash
# from a checkout of both repos as siblings
git clone https://github.com/pragmasg/pragmas-sdk.git
git clone https://github.com/pragmasg/pragmas-cli.git

cd pragmas-sdk && pip install -e .
cd ../pragmas-cli && pip install -e ".[dev]"
pytest
```

Requires Python 3.9+. Tests use Typer's `CliRunner` against the real
`analyze`/`market` local execution paths (pandas/matplotlib, a fake
DuckDuckGo backend) and mock the HTTP layer for `login` with
[`respx`](https://lundberg.github.io/respx/) — no live backend required for
either.

If you're changing something that depends on an in-development SDK change,
install it editable from a local checkout (`pip install -e ../pragmas-sdk`)
rather than waiting on a PyPI release.

## Opening a PR

1. For a genuinely new command, open an issue first (see above) — saves you
   from building something that duplicates SDK logic or targets a backend
   that isn't live yet.
2. Branch off `master`, keep the PR focused on one thing.
3. Make sure `pytest` passes locally.
4. If you touched the commands table in `README.md`, make sure the status
   (🟢/🟡/⚪) you claimed is accurate.
5. Test on more than one platform if you can, especially for anything
   touching console output — this CLI has already hit real Windows-specific
   bugs (legacy codepage issues with Unicode, `OSError`/`EINVAL` instead of
   `BrokenPipeError` on early pipe close) that a POSIX-only test run
   wouldn't catch.

There's no CLA — contributions are accepted under this repo's MIT license
(see [LICENSE](./LICENSE)), same as the rest of the code.

## Reporting bugs / requesting features

Run `pragmas feedback --open`, or
[open an issue directly](https://github.com/pragmasg/pragmas-cli/issues).

## Code of conduct

Be respectful, assume good faith, keep discussion about the code and the
product. Anything else gets moderated on a normal-sense basis — there's no
separate formal policy document for this repo yet.
