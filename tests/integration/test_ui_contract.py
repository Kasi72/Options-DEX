"""Contract tests for the Streamlit research-only interface."""

import sqlite3
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace


def test_streamlit_layer_has_no_collection_or_calculation_dependencies() -> None:
    source = Path("src/nifty_signal_engine/streamlit_app.py").read_text()
    forbidden = ["requests.", "httpx.", "time.sleep", "brentq", "norm.cdf", "open("]
    assert not any(token in source for token in forbidden)
    assert "SnapshotRepository" not in source


def test_dashboard_defaults_to_research_no_trade_without_promotion() -> None:
    from nifty_signal_engine.streamlit_app import research_action

    result = research_action(None)

    assert result["mode"] == "RESEARCH"
    assert result["action"] == "NO_TRADE"
    assert "MODEL_NOT_PROMOTED" in result["reasons"]


def test_precision_is_hidden_without_complete_governance_evidence() -> None:
    from nifty_signal_engine.streamlit_app import precision_display

    result = precision_display(
        {
            "precision": 0.75,
            "coverage": 0.2,
            "sample_count": 4,
            "precision_ci": (0.3, 0.95),
        }
    )

    assert result["precision"] is None
    assert result["reason"] == "INSUFFICIENT_EVIDENCE"


def test_malformed_precision_interval_fails_closed() -> None:
    from nifty_signal_engine.streamlit_app import precision_display

    result = precision_display(
        {
            "precision": 0.75,
            "coverage": 0.2,
            "sample_count": 40,
            "precision_ci": ("bad", 0.95),
            "evaluation_window": "2026-08-01/2026-08-26",
        }
    )

    assert result == {"precision": None, "reason": "INVALID_EVIDENCE"}


def test_invalid_health_suppresses_all_evidence_panels() -> None:
    from nifty_signal_engine.streamlit_app import build_dashboard_snapshot

    stamp = datetime.fromisoformat("2026-08-26T10:00:00+05:30")

    class Repository:
        def session_quality_summary(self, _instrument: str, _date: date) -> dict[str, object]:
            return {"snapshot_count": 1, "tradable_count": 0, "codes": {"STALE_SOURCE": 1}}

        def iter_audited_session(self, _instrument: str, _date: date):
            quality = SimpleNamespace(tradable=False, codes=("STALE_SOURCE",))
            return ((SimpleNamespace(source_timestamp=stamp), quality),)

    class Reader:
        def latest(self, _instrument: str) -> dict[str, object]:
            return {
                "instrument": "NIFTY",
                "structural_exposure": {"net_gex_rupees": 1},
                "probabilities": {"30m": {"up": 1}},
            }

    view = build_dashboard_snapshot(
        Repository(), "NIFTY", date(2026, 8, 26), feature_reader=Reader(), research_reader=Reader()
    )

    assert view.structural_exposure == {}
    assert view.probabilities == {}
    assert "STALE_SOURCE" in view.action["reasons"]


def test_json_readers_and_reports_require_explicit_matching_instrument(tmp_path: Path) -> None:
    from nifty_signal_engine.streamlit_app import JsonFeatureReader, load_reports

    (tmp_path / "old.json").write_text(
        '{"instrument":"BANKNIFTY","available_at":"2026-08-26T10:00:00+05:30"}', encoding="utf-8"
    )
    (tmp_path / "new.json").write_text(
        '{"instrument":"NIFTY","available_at":"2026-08-26T10:01:00+05:30","structural_exposure":{"net_gex_rupees":1}}',
        encoding="utf-8",
    )
    (tmp_path / "unscoped.json").write_text(
        '{"report_id":"unscoped"}', encoding="utf-8"
    )

    latest = JsonFeatureReader(tmp_path).latest("NIFTY")

    assert latest is not None
    assert latest["instrument"] == "NIFTY"
    assert [item["report_id"] for item in load_reports(tmp_path, "NIFTY") if "report_id" in item] == []


def test_local_repository_reads_health_without_schema_mutation(tmp_path: Path) -> None:
    from nifty_signal_engine.streamlit_app import LocalReadOnlyRepository

    database = tmp_path / "market.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE normalized_snapshots (
                id INTEGER PRIMARY KEY, source_timestamp TEXT NOT NULL,
                instrument TEXT NOT NULL, session_date TEXT NOT NULL
            );
            CREATE TABLE quality_decisions (
                id INTEGER PRIMARY KEY, normalized_snapshot_id INTEGER NOT NULL,
                tradable INTEGER NOT NULL, codes TEXT NOT NULL,
                details TEXT NOT NULL, checked_at TEXT NOT NULL
            );
            INSERT INTO normalized_snapshots VALUES
                (1, '2026-08-26T10:00:00+05:30', 'NIFTY', '2026-08-26');
            INSERT INTO quality_decisions VALUES
                (1, 1, 1, '[]', '{}', '2026-08-26T10:00:01+05:30');
            """
        )
        connection.commit()
    finally:
        connection.close()
    before = database.read_bytes()

    summary = LocalReadOnlyRepository(database).session_quality_summary(
        "NIFTY", date(2026, 8, 26)
    )

    assert summary == {"snapshot_count": 1, "tradable_count": 1, "codes": {}}
    assert database.read_bytes() == before
