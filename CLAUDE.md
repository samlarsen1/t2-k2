# T2-K2

uv project (Python 3.12), `src/t2k2`: fetches Trading212 holdings, flags overbought ones and harvests part of their gains with sell orders. Sell-side sibling of t2-d2.

## Commands

```bash
.venv/bin/pytest                         # tests (tests/)
.venv/bin/pre-commit run --all-files     # detect-secrets, ruff, bandit, semgrep, pip-audit, mypy --strict, pytest
uv sync                                  # install/update deps (uv.lock)
uv run t2k2-harvest [--execute] [--strategy FILE] [--report FILE]   # dry run unless --execute
```

Scheduled weekly, Tuesday 22:00 Europe/London, via the systemd timer in README (Scheduling).

## Rules

- **README.md is the developer entry point**: any new run/setup/test/lint command, strategy key, output or env var must be documented there with copy-paste-ready commands.
- **Never commit secrets.** Secrets live in `.env` (gitignored); document vars in `.env.example` with placeholders. Don't log exception messages that may contain sensitive data. Tests must never read `.env`.
- **Selling is real money.** Dry run stays the default; orders only with `--execute`. Never retry an order POST after a timeout or 5xx (only 429). Keep the pending-sell skip so re-runs can't double-sell, and only ever cancel T2-K2's own (`initiatedFrom == "API"`) expired sells.
- Decision logic in `strategy.py` stays pure (no I/O); thresholds belong in `config/strategy.yaml`, not code.
- Respect Trading212 rate limits (order placement 1 req/2s); don't add loops that hammer the API.
- Style: ruff (line length 100), full type hints passing `mypy --strict`, Google-style docstrings on public APIs, structured logging, specific exceptions — never swallow silently.
- Tests: add for every feature/fix under `tests/`. Mock all external APIs with realistic payloads, including error cases (timeouts, 5xx, rate limits, order rejections).
- Git: atomic commits, clear messages, no force-push to shared branches. Commit only on `feature/<name>`, `bug/<name>`, `task/<name>` or `hotfix/<name>` branches (lowercase-with-hyphens); never directly on `main`.
- Keep changes scoped — no unrelated refactors alongside fixes.
