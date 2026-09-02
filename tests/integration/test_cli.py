import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from nifty_signal_engine.data.repositories import SnapshotRepository

ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "dhan_option_chain.json"


def test_collect_once_fixture_is_persisted_and_reported_research_only(
    tmp_path: Path,
) -> None:
    """Removing fixture collection or the non-tradable label must not look like live trading."""
    data_dir = tmp_path / "data"
    command = [
        sys.executable,
        "-m",
        "nifty_signal_engine.cli",
        "collect-once",
        "--fixture",
        str(FIXTURE),
        "--instrument",
        "NIFTY",
        "--captured-at",
        "2026-08-30T10:00:00+05:30",
        "--data-dir",
        str(data_dir),
    ]

    completed = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, check=True
    )
    payload = json.loads(completed.stdout)

    assert payload["status"] == "COLLECTED"
    assert payload["research_only"] is True
    assert payload["raw_snapshot_id"]
    assert "token" not in completed.stdout.lower()

    inspected = subprocess.run(
        [
            sys.executable,
            "-m",
            "nifty_signal_engine.cli",
            "inspect-session",
            "--instrument",
            "NIFTY",
            "--session-date",
            "2026-08-30",
            "--data-dir",
            str(data_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    inspection = json.loads(inspected.stdout)
    assert inspection["instrument"] == "NIFTY"
    assert inspection["session_date"] == "2026-08-30"
    assert inspection["snapshot_count"] == 1
    assert inspection["tradable_count"] == 0
    assert inspection["codes"]["BASELINE_UNAVAILABLE"] == 1


def test_collection_failure_exits_nonzero_without_echoing_fixture_contents(
    tmp_path: Path,
) -> None:
    """Returning zero for a failed collection must not make automation treat it as success."""
    fixture = tmp_path / "malformed.json"
    fixture.write_text('{"secret":"not-a-token"}')
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "nifty_signal_engine.cli",
            "collect-once",
            "--fixture",
            str(fixture),
            "--instrument",
            "NIFTY",
            "--captured-at",
            "2026-08-30T10:00:00+05:30",
            "--data-dir",
            str(tmp_path / "data"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "not-a-token" not in completed.stdout
    assert "not-a-token" not in completed.stderr


def test_fixture_metadata_controls_capture_source_expiry_and_calendar_without_wall_clock(
    tmp_path: Path,
) -> None:
    """Hardcoding fixture expiry or a wall-clock receipt must not change its replay contract."""
    fixture_payload = json.loads(FIXTURE.read_text())
    fixture_payload["fixture_metadata"] = {
        "captured_at": "2026-08-30T10:00:01+05:30",
        "broker_source_timestamp": "2026-08-30T10:00:00+05:30",
        "source_time_authoritative": True,
        "expiry": "2026-09-02",
        "trading_dates": ["2026-08-30"],
    }
    fixture_payload["data"]["timestamp"] = "2026-08-30T10:00:00+05:30"
    fixture = tmp_path / "metadata-fixture.json"
    fixture.write_text(json.dumps(fixture_payload))
    data_dir = tmp_path / "data"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "nifty_signal_engine.cli",
            "collect-once",
            "--fixture",
            str(fixture),
            "--instrument",
            "NIFTY",
            "--data-dir",
            str(data_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    with SnapshotRepository(
        database_path=data_dir / "market.sqlite3", parquet_root=data_dir / "parquet"
    ) as repository:
        snapshots = list(
            repository.iter_session("NIFTY", date.fromisoformat("2026-08-30"))
        )

    assert json.loads(completed.stdout)["status"] == "COLLECTED"
    assert snapshots[0].expiry.isoformat() == "2026-09-02"
    assert snapshots[0].source_time_authoritative is True


def test_importing_cli_creates_no_data_directory_or_network_work(tmp_path: Path) -> None:
    """Moving collection into import-time code must not alter a caller's working directory."""
    completed = subprocess.run(
        [sys.executable, "-c", "import nifty_signal_engine.cli"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout == ""
    assert completed.stderr == ""
    assert not (tmp_path / ".nifty-signal-data").exists()
