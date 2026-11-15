# T2-K2 — Gain Harvester

Fetches your current Trading212 holdings, checks each one against configurable **overbought**
rules, and trims the overbought winners to lock in part of their gains.

Sibling of `t2-d2`, which buys dips. T2-K2 is the sell side,
with the same guardrails. **Dry run by default**: nothing is sold unless you pass `--execute`.

## Part of the T2 system

This repo is one of the T2 droids, coordinated from the `t2-hq` repo, which holds the system
architecture (`ARCHITECTURE.md`) and plan (`PLAN.md`). All the T2 repos live under the same
GitHub owner as this one.

| Repo | Role |
|---|---|
| `t2-d2` | buys dips; places buy orders on its own timers |
| **t2-k2** (this repo) | harvests gains from overbought holdings; places sell orders |
| `t2-bb8` | harvests the Trading212 instrument list; metadata only |
| `t2-3po` | advisory council; reviews theses, strategy and configuration; read-only |
| `t2-hq` | hub: plan, architecture, and the orchestrator and Telegram interface (planned) |

T2-K2 is the sell side of the execution layer. It trades by the rules in `config/strategy.yaml`, with no LLM and no approval step in the order path. `t2-3po` reads that file to suggest changes; a suggestion only takes effect as a reviewed change to it. It runs on its own and doesn't need the other repos to be running.

---

## How it works

```
GET pending orders ──► cancel T2-K2's expired sells ──► GET positions ──► gain ≥ min_gain_pct?
  ──► Yahoo daily closes ──► overbought signals ──► size order ──► (dry run | place sell)
```

1. **Expired orders**: `GET /api/v0/equity/orders` (pending). T2-K2's own limit sells (placed
   through the API) that are `order_expiry_days` old are cancelled, so their holdings are
   re-evaluated at today's price. Orders you placed in the app are never touched.
2. **Holdings**: `GET /api/v0/equity/positions`, read after cancelling so the shares those orders
   reserved can be sold again.
3. **Gain gate**: positions less than `harvest.min_gain_pct` above their average price are held
   without fetching any price data. Excluded tickers and tickers with a pending sell are skipped.
4. **Overbought check** on ~1 year of daily closes from Yahoo Finance. Each signal can be
   disabled; a holding is overbought when at least `min_signals` fire:

   | Signal | Default | Fires when |
   |--------|---------|------------|
   | RSI | `rsi_min: 70`, `rsi_period: 14` | Wilder RSI ≥ 70 |
   | Stretch above SMA | `min_pct_above_sma: 15`, `sma_period: 50` | close ≥ 15% above the 50-day SMA |
   | Run-up | `min_run_up_pct: 20`, `run_up_days: 20` | close up ≥ 20% over 20 sessions |

5. **Sizing** — sell `gain_fraction` of the unrealised gain (account currency, so FX is
   included), capped at `max_position_sell_pct` of the position and at the shares available to
   trade (shares in pies can't be sold), rounded down to `quantity_decimals`. Orders under
   `min_order_value` are skipped. `gain_fraction: 1.0` sells exactly the profit and leaves the
   original cost invested.
6. **Orders** — limit sells at Trading212's current price × (1 + `limit_offset_pct`%; 0 by
   default, i.e. at the current price), or market sells. Biggest gains first, at most
   `max_orders_per_run`, spaced 2.5s apart for Trading212's rate limit. A rejected order is
   reported and the rest still go ahead.

### Order expiry

Trading212's API has no expiry-date option: orders are `DAY` or `GOOD_TILL_CANCEL`. For a
week-long order, T2-K2 places `GOOD_TILL_CANCEL` and cancels it itself once it is
`order_expiry_days` (default 7) old. That only happens when T2-K2 runs, so expiry is as precise
as the schedule: with the weekly run below, each run cancels last week's unfilled orders and
places fresh ones if the holding is still overbought. Orders up to an hour short of the age count
as expired, so a run at the same weekday and time always catches last week's orders.

What a week-long limit at the current price does: if the price at the next open is at or above
the limit, it sells straight away (at the market price or better). If the price gaps down, it
waits and only sells if the price recovers to the limit within the week.

A dry run lists the orders it would cancel and the orders that would replace them. Shares
reserved by an order still count as unavailable in a dry run, so replacement quantities can show
smaller than a real run would place.

Order placement is never retried after a timeout or 5xx — the order may already exist, and a
retry could sell twice. Re-running is safe: tickers with a pending sell are skipped.

```
t2-k2/
├── config/strategy.yaml  # overbought + harvesting rules (edit this)
├── src/t2k2/
│   ├── cli.py            # `t2k2-harvest` entrypoint: table output, locks, run report, exit codes
│   ├── config.py         # env var settings (T212_*, T2K2_*)
│   ├── client.py         # Trading212 client: positions, pending orders, sell orders
│   ├── models.py         # pydantic models (extra attributes kept)
│   ├── market.py         # Yahoo Finance closes, T212→Yahoo symbols, RSI/SMA/% change
│   ├── strategy.py       # strategy file validation, overbought signals, order sizing (pure)
│   ├── harvest.py        # orchestration: fetch → evaluate → dry run / place
│   └── t2contract.py     # vendored from t2-hq: run reports, exit codes, locks
├── deploy/systemd/       # unit templates installed by t2-hq's bin/t2-deploy
└── tests/                # mocked-API tests
```

---

## Quick Start

### Prerequisites
- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager
- A Trading212 API key and secret. Evaluating needs the **Portfolio** and **Orders read**
  permissions; `--execute` also needs **Orders execute**. Try it on a demo account first.

### Local Setup

```bash
# Install
uv sync

# Copy and fill in secrets
cp .env.example .env

# Dry run: show what would be sold
uv run t2k2-harvest

# Dry run, writing the run report (indicators, signals, orders) to a chosen file
uv run t2k2-harvest --report reports/harvest.json

# Use another strategy file
uv run t2k2-harvest --strategy config/my-strategy.yaml

# Place the sell orders
uv run t2k2-harvest --execute
```

Example dry-run output (stdout; logs go to stderr):

```
T2-K2 harvest — DRY RUN (pass --execute to place orders) — 2026-11-14 09:00 UTC

TICKER          ACTION     GAIN%  ORDER                             REASON
--------------------------------------------------------------------------------------
NVDA_US_EQ      HARVEST    60.00  sell 1.87 @159.60 ~239.36 DRY_RUN overbought: RSI14 92.3 >= 70; ...
AAPL_US_EQ      HOLD        5.00                                    gain 5.00% below min_gain_pct 25%
```

### Run reports, exit codes and locks

T2-K2 follows the T2 droid contract (t2-hq `CONTRACT.md`):

- **Run report.** Every run writes a JSON report to `var/runs/<UTC timestamp>-<command>.json`
  (or `$T2_RUNS_DIR`). It records the version, command, start and end time, dry-run flag, status,
  exit code, a one-line summary and errors. Its `result` holds every holding's decision, indicators and order, and any cancelled orders. A sell order that couldn't be placed or cancelled makes the run `failed`. `--report PATH` writes it somewhere else.
- **Exit codes:** `0` success, `1` the run failed, `2` configuration or usage error, `3` skipped
  (a lock is held).
- **One run at a time.** A second run while one is in progress exits `3` at once.
- **The Trading212 account is shared.** Before calling Trading212 a run waits for the account lock,
  which every droid takes, so droids on independent timers don't trip the per-account rate limits.
  If it is still held after 10 minutes (`T2_T212_LOCK_TIMEOUT`, in seconds), the run exits `3`.

Locks live in `/run/user/<uid>/t2` (or `$T2_LOCK_DIR`).

```bash
uv run t2k2-harvest
ls var/runs/
```

### Scheduling (weekly, Tuesday 22:00 UK)

T2-K2 runs once a week, after the market close. At 22:00 UK time LSE (closes 16:30) and US
markets (21:00 UK, or 20:00 in the weeks when only one side has changed clocks) have closed and
the day's prices have settled, so the signals use Tuesday's final closes. Orders queue for
Wednesday's open.

The timer is installed by the deploy (see [Deployment](#deployment)). A run missed while the
machine was off is skipped rather than fired late (`Persistent=false`). Smoke-test by hand first,
ideally with `--execute` once against the demo account.

### Strategy

All thresholds live in [`config/strategy.yaml`](config/strategy.yaml), commented in place.
Unknown keys are rejected, so a typo fails the run instead of silently using a default.

- `exclude_tickers`: Trading212 tickers never to sell (core holdings).
- `yahoo_symbols`: fixes for tickers the automatic mapping gets wrong. The mapping handles
  `AAPL_US_EQ → AAPL`, `BRK_B_US_EQ → BRK-B`, `SHOP_CA_EQ → SHOP.TO` and lower-case exchange
  codes such as `VODl_EQ → VOD.L`, `SAPd_EQ → SAP.DE`, `MCp_EQ → MC.PA`, `ASMLa_EQ → ASML.AS`.
  A ticker it can't map is skipped and reported.

### Development

```bash
# Install and enable pre-commit hooks (run once after cloning)
uv sync
.venv/bin/pre-commit install

# Hooks run automatically on commit: detect-secrets, ruff, bandit, semgrep, pip-audit, mypy,
# pytest, plus a branch name check (see Branch naming below)
# To run manually against all files:
.venv/bin/pre-commit run --all-files
```

### Tests

```bash
.venv/bin/pytest -v
```

All Trading212 and Yahoo calls are mocked; tests never read `.env` or touch the network.

### Linters

```bash
.venv/bin/ruff check .
.venv/bin/mypy
```

### Branch naming

Commit on `feature/<name>`, `bug/<name>`, `task/<name>` or `hotfix/<name>` branches
(lowercase-with-hyphens), never directly on `main`. The `commit-msg` hook and the
`Branch Name` workflow enforce this.

### Dependency security

`uv` ignores package versions published in the last 7 days (`exclude-newer` in
`pyproject.toml`). `pip-audit` checks `uv.lock` on every commit and daily in CI
(`.github/workflows/dependency-audit.yml`). Dependabot proposes GitHub Actions updates weekly.

---

## Deployment

t2-k2 is deployed with t2-hq's `bin/t2-deploy`, which checks out a release tag in
`$T2_APPS/t2-k2`, builds its virtual environment and installs the systemd user units in
`deploy/systemd/`:

```bash
# From the t2-hq checkout
bin/t2-deploy release t2-k2 v0.2.0   # tag a clean, tested main and push the tag
bin/t2-deploy deploy t2-k2           # deploy the newest tag
bin/t2-deploy status
```

The units run `t2k2-harvest` every Tuesday at 22:00 UK time (`t2k2.timer`). **The deployed unit
is a dry run.** To let it place sell orders, add a drop-in, which survives redeploys:

```bash
systemctl --user edit t2k2.service
# add:
#   [Service]
#   Environment=T2K2_ARGS=--execute
systemctl --user cat t2k2.service     # check the override
```

Inspect runs:

```bash
systemctl --user list-timers t2k2.timer          # next run
journalctl --user -u t2k2.service -n 100         # last run's output
ls $T2_APPS/t2-k2/var/runs/                      # run reports
systemctl --user start t2k2.service              # run now
```

The deploy never creates, reads or edits `.env`: write `$T2_APPS/t2-k2/.env` from `.env.example`
first, or the deploy stops before enabling anything.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `T212_API_KEY` | yes | — | Trading212 API key |
| `T212_API_SECRET` | yes | — | Trading212 API secret |
| `T212_BASE_URL` | no | `https://live.trading212.com` | `https://demo.trading212.com` for the demo account; no other hosts are allowed |
| `T2K2_STRATEGY_FILE` | no | `config/strategy.yaml` | Strategy file; `--strategy` overrides it |
| `T2_RUNS_DIR` | no | `var/runs` | Where run reports go |
| `T2_LOCK_DIR` | no | `/run/user/<uid>/t2` | Where the run and Trading212 locks live |
| `T2_T212_LOCK_TIMEOUT` | no | `600` | Seconds to wait for the shared Trading212 lock before skipping |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `Trading212 API error 401` / `403` | Wrong key or secret, or the key lacks a permission (see Prerequisites). Live keys don't work against demo and vice versa. |
| `Trading212 API error: ... timed out; the order may have been placed` | Check open orders in the Trading212 app before re-running. A re-run skips tickers with a pending sell anyway. |
| `SKIP market data unavailable: no Yahoo symbol for ...` | Add the ticker under `yahoo_symbols` in the strategy file. |
| `SKIP only N closes; need M` | Recent listing; not enough history for the configured periods. |
| `SKIP rounded sell quantity is zero` | The position is small, or its shares are held in a pie. |
| `Strategy error: invalid strategy file` | The message names the bad key and why. |
| `Expired sell order ...: FAILED` | Usually the order filled or was cancelled in between. The holding is skipped this run and re-checked next week. |
| Expired orders never cancelled | Only T2-K2's own API-placed limit sells are cancelled, and only when T2-K2 runs. Check `order_expiry_days` isn't `null` and the timer is active. |

---

## Disclaimer

This is personal tooling, not financial advice. Selling realises gains; outside an ISA/SIPP that
can create a tax liability. Run dry first and check every order.
