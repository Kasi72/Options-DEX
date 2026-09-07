"""Thin, read-only Streamlit presentation for research evidence.

The dashboard is deliberately a consumer of persisted repository records and
already-produced feature, replay, signal, and validation reports.  It does not
collect data, persist anything, or turn a research result into an order.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from zoneinfo import ZoneInfo

from nifty_signal_engine.data.repositories import SnapshotRepository
from nifty_signal_engine.domain.signal import SignalAction
from nifty_signal_engine.monitoring.data_quality import DataQualityReport

try:  # Streamlit is an optional presentation dependency for library users.
    import streamlit as st
except ModuleNotFoundError:  # pragma: no cover - exercised by non-UI installs.
    st = None  # type: ignore[assignment]

IST = ZoneInfo("Asia/Kolkata")
Instrument = Literal["NIFTY", "BANKNIFTY"]


class FeatureReader(Protocol):
    """Read-only boundary for a previously materialized feature service."""

    def latest(self, instrument: str) -> Mapping[str, object] | None: ...


class ResearchReader(Protocol):
    """Read-only boundary for saved research probabilities and decisions."""

    def latest(self, instrument: str) -> Mapping[str, object] | None: ...


class ReportReader(Protocol):
    """Read-only boundary for replay and walk-forward evidence."""

    def reports(self, instrument: str) -> Sequence[Mapping[str, object]]: ...


class RepositoryReader(Protocol):
    """Small read-only subset of the local snapshot repository."""

    def session_quality_summary(self, instrument: str, session_date: date) -> Mapping[str, object]: ...

    def iter_audited_session(
        self, instrument: str, session_date: date
    ) -> Iterable[tuple[object, DataQualityReport]]: ...


@dataclass(frozen=True, slots=True)
class JsonReportReader:
    """Read-only adapter for precomputed replay/walk-forward JSON artifacts."""

    root: Path

    def reports(self, instrument: str) -> Sequence[Mapping[str, object]]:
        selected = cast(Instrument, instrument)
        return load_reports(self.root, selected)


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    """Serializable view-model used by renderers and tests."""

    instrument: Instrument
    session_date: date
    health: Mapping[str, object]
    latest_source_time: datetime | None
    last_valid_source_time: datetime | None
    structural_exposure: Mapping[str, object]
    incremental_exposure: Mapping[str, object]
    zero_levels: Mapping[str, object]
    probabilities: Mapping[str, object]
    reports: tuple[Mapping[str, object], ...]
    action: Mapping[str, object]


def research_action(promotion_metadata: object | None) -> dict[str, object]:
    """Return the UI action, which is always research-only and fail-closed."""
    reasons = ["RESEARCH_ONLY", "NO_ORDER_SUBMISSION"]
    if promotion_metadata is None:
        reasons.insert(0, "MODEL_NOT_PROMOTED")
    else:
        reasons.insert(0, "PROMOTION_METADATA_IS_NOT_EXECUTION_AUTHORITY")
    return {
        "mode": "RESEARCH",
        "action": SignalAction.NO_TRADE.value,
        "reasons": tuple(reasons),
    }


def precision_display(metrics: Mapping[str, object] | object) -> dict[str, object]:
    """Expose precision only when all governance evidence is present.

    The evaluation window is intentionally required even though older metric
    objects do not carry one.  This prevents a visually impressive number from
    being shown without the period on which it was measured.
    """
    values = _mapping_or_attributes(metrics)
    required = ("coverage", "sample_count", "precision_ci", "evaluation_window")
    if any(values.get(name) is None for name in required):
        return {"precision": None, "reason": "INSUFFICIENT_EVIDENCE"}
    interval = values["precision_ci"]
    if not isinstance(interval, (tuple, list)) or len(interval) != 2:
        return {"precision": None, "reason": "INSUFFICIENT_EVIDENCE"}
    coverage, sample_count = values["coverage"], values["sample_count"]
    if not isinstance(coverage, (int, float)) or not 0 <= coverage <= 1:
        return {"precision": None, "reason": "INVALID_EVIDENCE"}
    if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count <= 0:
        return {"precision": None, "reason": "INVALID_EVIDENCE"}
    precision = values.get("precision")
    if not isinstance(precision, (int, float)):
        return {"precision": None, "reason": "INSUFFICIENT_EVIDENCE"}
    return {
        "precision": float(precision),
        "coverage": float(coverage),
        "sample_count": sample_count,
        "precision_ci": (float(interval[0]), float(interval[1])),
        "evaluation_window": str(values["evaluation_window"]),
        "reason": None,
    }


def build_dashboard_snapshot(
    repository: RepositoryReader | None,
    instrument: Instrument,
    session_date: date,
    *,
    feature_reader: FeatureReader | None = None,
    research_reader: ResearchReader | None = None,
    report_reader: ReportReader | None = None,
) -> DashboardSnapshot:
    """Assemble a dashboard view without deriving or mutating market data."""
    health: dict[str, object] = {
        "snapshot_count": 0,
        "tradable_count": 0,
        "codes": {},
        "status": "NO_DATA",
    }
    latest_source_time: datetime | None = None
    last_valid_source_time: datetime | None = None
    if repository is not None:
        try:
            summary = repository.session_quality_summary(instrument, session_date)
            if isinstance(summary, Mapping):
                health.update({str(key): value for key, value in summary.items()})
                health["status"] = "HEALTHY" if not summary.get("codes") else "QUALITY_REVIEW"
            audited = tuple(repository.iter_audited_session(instrument, session_date))
            for snapshot, quality in audited:
                timestamp = getattr(snapshot, "source_timestamp", None)
                if isinstance(timestamp, datetime):
                    latest_source_time = timestamp
                if (
                    isinstance(quality, DataQualityReport)
                    and quality.tradable
                    and not quality.codes
                    and isinstance(timestamp, datetime)
                ):
                    last_valid_source_time = timestamp
        except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            health.update({"status": "UNAVAILABLE", "error": type(error).__name__})

    feature = _reader_latest(feature_reader, instrument)
    research = _reader_latest(research_reader, instrument)
    reports = tuple(report_reader.reports(instrument)) if report_reader is not None else ()
    return DashboardSnapshot(
        instrument=instrument,
        session_date=session_date,
        health=health,
        latest_source_time=latest_source_time,
        last_valid_source_time=last_valid_source_time,
        structural_exposure=_layer(feature, "structural_exposure"),
        incremental_exposure=_layer(feature, "incremental_exposure"),
        zero_levels=_layer(feature, "zero_levels"),
        probabilities=_layer(research, "probabilities"),
        reports=reports,
        action=research_action(_layer(research, "promotion_metadata") or None),
    )


def render_dashboard(snapshot: DashboardSnapshot) -> None:
    """Render all research panels for an already assembled view-model."""
    streamlit = _streamlit()
    streamlit.title("NIFTY / BANKNIFTY options research")
    streamlit.caption("Research and shadow monitoring only — no live execution")
    streamlit.subheader(f"{snapshot.instrument} · {snapshot.session_date.isoformat()}")

    _render_health(streamlit, snapshot)
    _render_exposure(streamlit, snapshot)
    _render_zero_levels(streamlit, snapshot.zero_levels)
    _render_probabilities(streamlit, snapshot.probabilities)
    _render_action(streamlit, snapshot.action)
    _render_reports(streamlit, snapshot.reports)


def main() -> None:
    """Start the local research dashboard using persisted local evidence."""
    streamlit = _streamlit()
    streamlit.set_page_config(page_title="NIFTY research", layout="wide")
    instrument = cast(Instrument, streamlit.sidebar.selectbox("Instrument", ["NIFTY", "BANKNIFTY"]))
    today = datetime.now(IST).date()
    selected = streamlit.sidebar.date_input("Session date", value=today)
    selected_date = selected if isinstance(selected, date) else today
    data_dir = Path(".nifty-signal-data")
    database = data_dir / "market.sqlite3"
    repository: SnapshotRepository | None = None
    if database.exists():
        repository = SnapshotRepository(database_path=database, parquet_root=data_dir / "parquet")
    try:
        view = build_dashboard_snapshot(
            repository,
            instrument,
            selected_date,
            report_reader=JsonReportReader(data_dir / "reports"),
        )
        render_dashboard(view)
    finally:
        if repository is not None:
            repository.close()


def _streamlit() -> Any:
    if st is None:
        raise RuntimeError("Streamlit is required to render this module")
    return st


def _mapping_or_attributes(value: Mapping[str, object] | object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    return {
        name: getattr(value, name)
        for name in ("precision", "coverage", "sample_count", "precision_ci", "evaluation_window")
        if hasattr(value, name)
    }


def _reader_latest(reader: object | None, instrument: str) -> Mapping[str, object] | None:
    if reader is None:
        return None
    latest = getattr(reader, "latest", None)
    value = latest(instrument) if callable(latest) else None
    return value if isinstance(value, Mapping) else None


def _layer(value: Mapping[str, object] | None, name: str) -> Mapping[str, object]:
    nested = value.get(name) if value is not None else None
    return dict(nested) if isinstance(nested, Mapping) else {}


def _render_health(streamlit: Any, snapshot: DashboardSnapshot) -> None:
    streamlit.subheader("Collection and data health")
    columns = streamlit.columns(4)
    columns[0].metric("Status", str(snapshot.health.get("status", "UNKNOWN")))
    columns[1].metric("Snapshots", str(snapshot.health.get("snapshot_count", 0)))
    columns[2].metric("Valid snapshots", str(snapshot.health.get("tradable_count", 0)))
    columns[3].metric("Last valid source", _format_timestamp(snapshot.last_valid_source_time))
    streamlit.caption(f"Latest source: {_format_timestamp(snapshot.latest_source_time)}")
    codes = snapshot.health.get("codes", {})
    if codes:
        streamlit.warning("Quality findings: " + ", ".join(f"{key} ({value})" for key, value in cast(Mapping[str, object], codes).items()))


def _render_exposure(streamlit: Any, snapshot: DashboardSnapshot) -> None:
    streamlit.subheader("Exposure layers")
    columns = streamlit.columns(2)
    _render_exposure_layer(columns[0], "Structural exposure", snapshot.structural_exposure)
    _render_exposure_layer(columns[1], "Incremental exposure", snapshot.incremental_exposure)


def _render_exposure_layer(container: Any, title: str, values: Mapping[str, object]) -> None:
    container.markdown(f"**{title}**")
    if not values:
        container.info("Unavailable — no persisted feature result for this session.")
        return
    for label in ("GEX", "DEX"):
        rupees = values.get(f"{label.lower()}_rupees", values.get(f"net_{label.lower()}_rupees"))
        crore = values.get(f"{label.lower()}_crore", values.get(f"net_{label.lower()}_crore"))
        container.write(f"Net {label}: {_format_number(rupees)} rupees · {_format_number(crore)} crore")


def _render_zero_levels(streamlit: Any, values: Mapping[str, object]) -> None:
    streamlit.subheader("Zero levels")
    if not values:
        streamlit.info("Unavailable — zero-level evidence was not persisted.")
        return
    for name, result in values.items():
        if isinstance(result, Mapping):
            status = str(result.get("status", "UNKNOWN"))
            level = result.get("level")
            reason = result.get("reason", status)
            streamlit.write(f"{name}: {status} · level={level if level is not None else 'null'} · reason={reason}")


def _render_probabilities(streamlit: Any, values: Mapping[str, object]) -> None:
    streamlit.subheader("Research probabilities")
    if not values:
        streamlit.info("Unavailable — no saved research probabilities.")
        return
    for horizon, probabilities in values.items():
        if isinstance(probabilities, Mapping):
            streamlit.write(f"{horizon}: P(up)={probabilities.get('up', '—')} · P(down)={probabilities.get('down', '—')} · P(no move)={probabilities.get('no_move', '—')}")


def _render_action(streamlit: Any, action: Mapping[str, object]) -> None:
    streamlit.subheader("Current action")
    streamlit.error(f"{action.get('action', 'NO_TRADE')} · {action.get('mode', 'RESEARCH')}")
    streamlit.write("Reasons: " + ", ".join(str(reason) for reason in cast(Sequence[object], action.get("reasons", ()))))


def _render_reports(streamlit: Any, reports: Sequence[Mapping[str, object]]) -> None:
    streamlit.subheader("Replay and walk-forward reports")
    if not reports:
        streamlit.info("No replay or walk-forward reports are available.")
        return
    labels = [str(report.get("report_id", index)) for index, report in enumerate(reports)]
    selected = streamlit.selectbox("Report", labels)
    report = reports[labels.index(selected)]
    streamlit.json(dict(report))
    directions = report.get("directions")
    if isinstance(directions, Mapping):
        for direction in ("BUY", "SELL"):
            metrics = directions.get(direction)
            if isinstance(metrics, Mapping):
                streamlit.write(f"{direction} precision evidence")
                display = precision_display(metrics)
                if display["precision"] is None:
                    streamlit.info(f"{direction}: precision hidden ({display['reason']})")
                else:
                    streamlit.write(display)


def _format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(IST).isoformat(timespec="seconds")


def _format_number(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:,.2f}"
    return "—"


def load_reports(report_root: Path, instrument: Instrument) -> tuple[Mapping[str, object], ...]:
    """Load precomputed JSON reports without treating them as executable input."""
    if not report_root.exists() or not report_root.is_dir():
        return ()
    reports: list[Mapping[str, object]] = []
    for path in sorted(report_root.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping) and (value.get("instrument") in (None, instrument)):
            reports.append(dict(value))
    return tuple(reports)


if __name__ == "__main__":  # pragma: no cover - Streamlit invokes the script.
    main()
