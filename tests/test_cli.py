"""CLI wiring: exit codes and output, with the client and price source patched out."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from t2k2 import cli
from t2k2.client import Trading212Error

from .payloads import POSITIONS, rising_closes
from .test_harvest import FakeBroker, FakePrices

CREDS = {"T212_API_KEY": "k", "T212_API_SECRET": "s", "T212_BASE_URL": "https://demo.trading212.com"}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Never pick up a real .env: run from an empty directory with a controlled environment.
    monkeypatch.chdir(tmp_path)
    for name in ("T212_API_KEY", "T212_API_SECRET", "T212_BASE_URL", "T2K2_STRATEGY_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "load_dotenv", lambda: False)
    strategy = Path(__file__).resolve().parents[1] / "config" / "strategy.yaml"
    monkeypatch.setenv("T2K2_STRATEGY_FILE", str(strategy))


def patch_io(monkeypatch: pytest.MonkeyPatch, broker: FakeBroker) -> None:
    class Ctx:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def __enter__(self) -> FakeBroker:
            return broker

        def __exit__(self, *exc: object) -> None: ...

    monkeypatch.setattr(cli, "Trading212Client", Ctx)
    prices = FakePrices({"NVDA": rising_closes(), "VOD.L": rising_closes(start=80, end=104)})
    monkeypatch.setattr(cli, "YahooPriceHistory", lambda: prices)


def test_missing_config_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == cli.EXIT_CONFIG
    assert "T212_API_KEY" in capsys.readouterr().err


def test_bad_strategy_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for k, v in CREDS.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["--strategy", str(tmp_path / "missing.yaml")]) == cli.EXIT_CONFIG


def test_dry_run_prints_table_and_writes_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for k, v in CREDS.items():
        monkeypatch.setenv(k, v)
    broker = FakeBroker(POSITIONS)
    patch_io(monkeypatch, broker)
    report_path = tmp_path / "out" / "report.json"
    assert cli.main(["--report", str(report_path)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "NVDA_US_EQ" in out and "HARVEST" in out
    assert broker.placed == []
    assert json.loads(report_path.read_text())["execute"] is False


def test_execute_with_failed_order_exits_5(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in CREDS.items():
        monkeypatch.setenv(k, v)
    broker = FakeBroker(POSITIONS, reject={"NVDA_US_EQ"})
    patch_io(monkeypatch, broker)
    monkeypatch.setattr("t2k2.harvest.time.sleep", lambda _: None)
    assert cli.main(["--execute"]) == cli.EXIT_ORDERS_FAILED
    assert [p[0] for p in broker.placed] == ["VODl_EQ"]


def test_api_failure_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in CREDS.items():
        monkeypatch.setenv(k, v)

    class Down(FakeBroker):
        def get_positions(self) -> list:  # type: ignore[type-arg]
            raise Trading212Error(401, "unauthorised")

    patch_io(monkeypatch, Down([]))
    assert cli.main([]) == cli.EXIT_API
