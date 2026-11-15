"""CLI wiring: exit codes and output, with the client and price source patched out."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from t2k2 import cli, t2contract
from t2k2.client import Trading212Error

from .payloads import POSITIONS, rising_closes
from .test_harvest import FakeBroker, FakePrices

CREDS = {"T212_API_KEY": "k", "T212_API_SECRET": "s", "T212_BASE_URL": "https://demo.trading212.com"}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Never pick up a real .env: run from an empty directory with a controlled environment.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("T2_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("T2_RUNS_DIR", str(tmp_path / "runs"))
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
    report = json.loads(report_path.read_text())
    assert (report["droid"], report["status"], report["dry_run"]) == ("t2-k2", "ok", True)
    assert report["result"]["execute"] is False
    assert "sell orders simulated" in report["summary"]


def test_execute_with_failed_order_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in CREDS.items():
        monkeypatch.setenv(k, v)
    broker = FakeBroker(POSITIONS, reject={"NVDA_US_EQ"})
    patch_io(monkeypatch, broker)
    monkeypatch.setattr("t2k2.harvest.time.sleep", lambda _: None)
    assert cli.main(["--execute"]) == cli.EXIT_FAILED
    assert [p[0] for p in broker.placed] == ["VODl_EQ"]


def test_api_failure_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in CREDS.items():
        monkeypatch.setenv(k, v)

    class Down(FakeBroker):
        def get_positions(self) -> list:  # type: ignore[type-arg]
            raise Trading212Error(401, "unauthorised")

    patch_io(monkeypatch, Down([]))
    assert cli.main([]) == cli.EXIT_FAILED


def only_report(tmp_path: Path) -> dict[str, object]:
    reports = list((tmp_path / "runs").glob("*-harvest.json"))
    assert len(reports) == 1
    return json.loads(reports[0].read_text())  # type: ignore[no-any-return]


def test_every_run_writes_a_report_by_default(tmp_path: Path) -> None:
    assert cli.main([]) == cli.EXIT_CONFIG
    report = only_report(tmp_path)
    assert report["status"] == "config_error"
    assert report["dry_run"] is True


def test_failed_orders_are_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for k, v in CREDS.items():
        monkeypatch.setenv(k, v)
    patch_io(monkeypatch, FakeBroker(POSITIONS, reject={"NVDA_US_EQ"}))
    monkeypatch.setattr("t2k2.harvest.time.sleep", lambda _: None)
    cli.main(["--execute"])
    report = only_report(tmp_path)
    assert (report["status"], report["dry_run"]) == ("failed", False)
    assert report["errors"] == ["1 sell orders couldn't be placed or cancelled"]


@pytest.mark.parametrize("lock", ["run", "t212"])
def test_held_lock_skips_without_touching_the_broker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lock: str
) -> None:
    for k, v in CREDS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("T2_T212_LOCK_TIMEOUT", "0")
    broker = FakeBroker(POSITIONS)
    patch_io(monkeypatch, broker)
    holder = t2contract.run_lock("t2-k2") if lock == "run" else t2contract.t212_lock(0)
    # flock locks belong to an open file, so the run's own open conflicts with this one.
    with holder:
        assert cli.main(["--execute"]) == cli.EXIT_SKIPPED
    assert broker.placed == []
    assert only_report(tmp_path)["status"] == "skipped"
