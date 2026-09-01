import json
import subprocess
import sys
from pathlib import Path

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

    assert json.loads(inspected.stdout) == {
        "instrument": "NIFTY",
        "session_date": "2026-08-30",
        "snapshot_count": 1,
    }
