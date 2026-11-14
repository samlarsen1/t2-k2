# T2-K2 — Gain Harvester

Fetches your current Trading212 holdings, checks each one against configurable **overbought**
rules, and trims the overbought winners to lock in part of their gains.

Sibling of [T2-D2](https://github.com/samlarsen1/t2-d2), which buys dips. T2-K2 is the sell side,
with the same guardrails. **Dry run by default**: nothing is sold unless you pass `--execute`.

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
`5` one or more sell orders couldn't be placed or cancelled.

### Scheduling (weekly, Tuesday 22:00 UK)

T2-K2 runs once a week, after the market close. At 22:00 UK time LSE (closes 16:30) and US
markets (21:00 UK, or 20:00 in the weeks when only one side has changed clocks) have closed and
the day's prices have settled, so the signals use Tuesday's final closes. Orders queue for
Wednesday's open.

Smoke-test by hand first (and ideally `--execute` once against the demo account):

```bash
uv run t2k2-harvest
```

Install the systemd service and timer. Paste this from the repo directory; `$USER` and `$PWD`
are filled in as you paste:

```bash
sudo tee /etc/systemd/system/t2k2-harvest.service >/dev/null <<UNIT
[Unit]
Description=T2-K2 weekly gain harvest
Wants=network-online.target
After=network-online.target time-sync.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$PWD
ExecStart=$PWD/.venv/bin/t2k2-harvest --execute --report reports/latest.json
TimeoutStartSec=15min
UNIT

sudo tee /etc/systemd/system/t2k2-harvest.timer >/dev/null <<UNIT
[Unit]
Description=T2-K2 weekly gain harvest (Tuesday 22:00 UK)

[Timer]
OnCalendar=Tue 22:00 Europe/London
Unit=t2k2-harvest.service
AccuracySec=1s
Persistent=false

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now t2k2-harvest.timer
```

`Persistent=false` skips a run missed while the machine was off, rather than firing it late.
Check and inspect:

```bash
systemctl list-timers t2k2-harvest.timer      # next run
journalctl -u t2k2-harvest.service -n 100     # last run's output
cat reports/latest.json                       # last run's full report
sudo systemctl start t2k2-harvest.service     # run now
```

To change the time, edit `OnCalendar` (check it with
`systemd-analyze calendar "Tue 22:00 Europe/London"`), then run `sudo systemctl daemon-reload`.
To stop it: `sudo systemctl disable --now t2k2-harvest.timer`.

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
| `Expired sell order ...: FAILED` | Usually the order filled or was cancelled in between. The holding is skipped this run and re-checked next week. |
| Expired orders never cancelled | Only T2-K2's own API-placed limit sells are cancelled, and only when T2-K2 runs. Check `order_expiry_days` isn't `null` and the timer is active. |

---

## Disclaimer

This is personal tooling, not financial advice. Selling realises gains; outside an ISA/SIPP that
can create a tax liability. Run dry first and check every order.
