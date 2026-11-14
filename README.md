# T2-K2 — Gain Harvester

Fetches your current Trading212 holdings, checks each one against configurable **overbought**
rules, and trims the overbought winners to lock in part of their gains.

Sibling of [T2-D2](https://github.com/samlarsen1/t2-d2), which buys dips. T2-K2 is the sell side,
with the same guardrails. **Dry run by default**: nothing is sold unless you pass `--execute`.

---

## How it works

```
GET positions ──► gain ≥ min_gain_pct? ──► Yahoo daily closes ──► overbought signals ──► size order ──► (dry run | place sell)
GET pending orders ─┘ (skip tickers that already have a pending sell)
```

1. **Holdings**: `GET /api/v0/equity/positions` and `GET /api/v0/equity/orders` (pending).
2. **Gain gate**: positions less than `harvest.min_gain_pct` above their average price are held
   without fetching any price data. Excluded tickers and tickers with a pending sell are skipped.
3. **Overbought check** on ~1 year of daily closes from Yahoo Finance. Each signal can be
   disabled; a holding is overbought when at least `min_signals` fire:

   | Signal | Default | Fires when |
   |--------|---------|------------|
   | RSI | `rsi_min: 70`, `rsi_period: 14` | Wilder RSI ≥ 70 |
   | Stretch above SMA | `min_pct_above_sma: 15`, `sma_period: 50` | close ≥ 15% above the 50-day SMA |
   | Run-up | `min_run_up_pct: 20`, `run_up_days: 20` | close up ≥ 20% over 20 sessions |

4. **Sizing** — sell `gain_fraction` of the unrealised gain (account currency, so FX is
   included), capped at `max_position_sell_pct` of the position and at the shares available to
   trade (shares in pies can't be sold), rounded down to `quantity_decimals`. Orders under
   `min_order_value` are skipped. `gain_fraction: 1.0` sells exactly the profit and leaves the
   original cost invested.
5. **Orders** — limit sells at Trading212's current price × (1 + `limit_offset_pct`%), or market
   sells. Biggest gains first, at most `max_orders_per_run`, spaced 2.5s apart for Trading212's
   rate limit. A rejected order is reported and the rest still go ahead.

Order placement is never retried after a timeout or 5xx — the order may already exist, and a
retry could sell twice. Re-running is safe: tickers with a pending sell are skipped.

```
t2-k2/
├── config/strategy.yaml  # overbought + harvesting rules (edit this)
├── src/t2k2/
│   ├── cli.py            # `t2k2-harvest` entrypoint, table/JSON output, exit codes
│   ├── config.py         # env var settings (T212_*, T2K2_*)
│   ├── client.py         # Trading212 client: positions, pending orders, sell orders
│   ├── models.py         # pydantic models (extra attributes kept)
│   ├── market.py         # Yahoo Finance closes, T212→Yahoo symbols, RSI/SMA/% change
│   ├── strategy.py       # strategy file validation, overbought signals, order sizing (pure)
│   └── harvest.py        # orchestration: fetch → evaluate → dry run / place
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

# Copy and fill in secrets (or symlink the same .env T2-D2 uses)
cp .env.example .env

# Dry run: show what would be sold
uv run t2k2-harvest

# Dry run and save the full report (indicators, signals, orders) as JSON
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

Exit codes: `0` success, `2` configuration or strategy file error, `3` Trading212 API error,
`5` one or more sell orders failed.

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

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `T212_API_KEY` | yes | — | Trading212 API key |
| `T212_API_SECRET` | yes | — | Trading212 API secret |
| `T212_BASE_URL` | no | `https://live.trading212.com` | `https://demo.trading212.com` for the demo account; no other hosts are allowed |
| `T2K2_STRATEGY_FILE` | no | `config/strategy.yaml` | Strategy file; `--strategy` overrides it |
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

---

## Disclaimer

This is personal tooling, not financial advice. Selling realises gains; outside an ISA/SIPP that
can create a tax liability. Run dry first and check every order.
