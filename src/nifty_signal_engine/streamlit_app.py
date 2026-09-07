"""Thin, read-only Streamlit presentation for research evidence.

The dashboard is deliberately a consumer of persisted repository records and
already-produced feature, replay, signal, and validation reports.  It does not
collect data, persist anything, or turn a research result into an order.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Protocol, cast
from zoneinfo import ZoneInfo

from nifty_signal_engine.domain.signal import SignalAction
from nifty_signal_engine.monitoring.data_quality import (
    DataQualityCode,
    DataQualityReport,
)

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
class LocalReadOnlyRepository:
    """Read health evidence through SQLite's immutable read-only URI.

    This adapter intentionally does not construct the writer repository. Schema
    creation and recovery belong to the collection process, not presentation.
    """

    database: Path

    def _historical_rows(self, instrument: str, session_date: date) -> tuple[sqlite3.Row, ...]:
        if not self.database.exists():
            return ()
        uri = f"file:{self.database.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT n.id, n.source_timestamp, q.tradable, q.codes,
                       q.details, q.checked_at
                FROM normalized_snapshots AS n
                LEFT JOIN quality_decisions AS q
                  ON q.normalized_snapshot_id = n.id
                WHERE n.instrument = ? AND n.session_date = ?
                ORDER BY n.source_timestamp, n.id, q.id
                """,
                (instrument, session_date.isoformat()),
            ).fetchall()
        finally:
            connection.close()
        return tuple(rows)

    def iter_audited_session(
        self, instrument: str, session_date: date
    ) -> Iterable[tuple[object, DataQualityReport]]:
        selected: dict[int, tuple[str, object, DataQualityReport]] = {}
        for row in self._historical_rows(instrument, session_date):
            snapshot_id = int(row["id"])
            if snapshot_id in selected:
                continue
            details = _json_mapping(row["details"])
            if row["tradable"] is None or details.get("decision_scope") is not None:
                continue
            source = _parse_stored_timestamp(row["source_timestamp"])
            checked = _parse_stored_timestamp(row["checked_at"])
            report = _stored_quality_report(row["tradable"], row["codes"], details, checked)
            selected[snapshot_id] = (str(row["source_timestamp"]), SimpleNamespace(source_timestamp=source), report)
        for _, snapshot, quality in sorted(selected.values(), key=lambda item: item[0]):
            yield snapshot, quality

    def session_quality_summary(self, instrument: str, session_date: date) -> Mapping[str, object]:
        audited = tuple(self.iter_audited_session(instrument, session_date))
        snapshot_ids = {int(row["id"]) for row in self._historical_rows(instrument, session_date)}
        codes: dict[str, int] = {}
        tradable_count = 0
        for _, quality in audited:
            tradable_count += int(quality.tradable and not quality.codes)
            for code in quality.codes:
                codes[code.value] = codes.get(code.value, 0) + 1
        missing = len(snapshot_ids) - len(audited)
        if missing > 0:
            codes[DataQualityCode.QUALITY_MISSING.value] = missing
        return {
            "snapshot_count": len(snapshot_ids),
            "tradable_count": tradable_count,
            "codes": codes,
        }


@dataclass(frozen=True, slots=True)
class JsonReportReader:
    """Read-only adapter for precomputed replay/walk-forward JSON artifacts."""

    root: Path

    def reports(self, instrument: str) -> Sequence[Mapping[str, object]]:
        selected = cast(Instrument, instrument)
        return load_reports(self.root, selected)


@dataclass(frozen=True, slots=True)
class JsonFeatureReader:
    """Read the latest precomputed feature view from local JSON evidence."""

    root: Path

    def latest(self, instrument: str) -> Mapping[str, object] | None:
        return _latest_json_evidence(self.root, instrument)


@dataclass(frozen=True, slots=True)
class JsonResearchReader:
    """Read the latest precomputed research probabilities from local evidence."""

    root: Path

    def latest(self, instrument: str) -> Mapping[str, object] | None:
        return _latest_json_evidence(self.root, instrument)


@dataclass(frozen=True, slots=True)
class SqliteResearchReader:
    """Read persisted research signal payloads without opening a writer."""

    database: Path

    def latest(self, instrument: str) -> Mapping[str, object] | None:
        return _latest_sqlite_evidence(self.database, instrument, field=None)


@dataclass(frozen=True, slots=True)
class SqliteFeatureReader:
    """Read feature views embedded in saved research evidence, if present."""

    database: Path

    def latest(self, instrument: str) -> Mapping[str, object] | None:
        return _latest_sqlite_evidence(self.database, instrument, field="features")


@dataclass(frozen=True, slots=True)
class ChainedReader:
    """Try local materialized evidence sources in deterministic priority order."""

    readers: tuple[object, ...]

    def latest(self, instrument: str) -> Mapping[str, object] | None:
        for reader in self.readers:
            value = _reader_latest(reader, instrument)
            if value is not None:
                return value
        return None


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


def research_action(
    promotion_metadata: object | None, *, blockers: Sequence[str] = ()
) -> dict[str, object]:
    """Return the UI action, which is always research-only and fail-closed."""
    reasons = [str(reason) for reason in blockers]
    reasons.extend(("RESEARCH_ONLY", "NO_ORDER_SUBMISSION"))
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
    try:
        lower, upper = float(interval[0]), float(interval[1])
    except (TypeError, ValueError, OverflowError):
        return {"precision": None, "reason": "INVALID_EVIDENCE"}
    if not math.isfinite(lower) or not math.isfinite(upper) or not 0 <= lower <= upper <= 1:
        return {"precision": None, "reason": "INVALID_EVIDENCE"}
    coverage, sample_count = values["coverage"], values["sample_count"]
    if not isinstance(coverage, (int, float)) or not 0 <= coverage <= 1:
        return {"precision": None, "reason": "INVALID_EVIDENCE"}
    if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count <= 0:
        return {"precision": None, "reason": "INVALID_EVIDENCE"}
    precision = values.get("precision")
    if not isinstance(precision, (int, float)) or not math.isfinite(float(precision)):
        return {"precision": None, "reason": "INSUFFICIENT_EVIDENCE"}
    return {
        "precision": float(precision),
        "coverage": float(coverage),
        "sample_count": sample_count,
        "precision_ci": (lower, upper),
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

    quality_ok = (
        health.get("status") == "HEALTHY"
        and _count(health.get("snapshot_count", 0)) > 0
        and _count(health.get("tradable_count", 0))
        == _count(health.get("snapshot_count", 0))
        and last_valid_source_time is not None
    )
    feature = _reader_latest(feature_reader, instrument) if quality_ok else None
    research = _reader_latest(research_reader, instrument) if quality_ok else None
    reports_raw = tuple(report_reader.reports(instrument)) if report_reader is not None else ()
    reports = tuple(
        dict(report)
        for report in reports_raw
        if isinstance(report, Mapping) and report.get("instrument") == instrument
    )
    blockers = () if quality_ok else tuple(_quality_blockers(health))
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
        action=research_action(
            _layer(research, "promotion_metadata") or None, blockers=blockers
        ),
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
    repository: LocalReadOnlyRepository | None = None
    if database.exists():
        repository = LocalReadOnlyRepository(database)
    view = build_dashboard_snapshot(
        repository,
        instrument,
        selected_date,
        feature_reader=ChainedReader(
            (JsonFeatureReader(data_dir / "features"), SqliteFeatureReader(database))
        ),
        research_reader=ChainedReader(
            (JsonResearchReader(data_dir / "research"), SqliteResearchReader(database))
        ),
        report_reader=JsonReportReader(data_dir / "reports"),
    )
    render_dashboard(view)


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
    if not isinstance(value, Mapping) or value.get("instrument") != instrument:
        return None
    quality = value.get("quality")
    if isinstance(quality, Mapping) and (
        quality.get("tradable") is not True or quality.get("codes")
    ):
        return None
    return value


def _quality_blockers(health: Mapping[str, object]) -> tuple[str, ...]:
    codes = health.get("codes")
    if isinstance(codes, Mapping) and codes:
        return tuple(str(code) for code in codes)
    return ("QUALITY_NOT_VALID",)


def _layer(value: Mapping[str, object] | None, name: str) -> Mapping[str, object]:
    nested = value.get(name) if value is not None else None
    return dict(nested) if isinstance(nested, Mapping) else {}


def _json_mapping(value: object) -> dict[str, object]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return {str(key): item for key, item in parsed.items()} if isinstance(parsed, Mapping) else {}
    return {str(key): item for key, item in value.items()} if isinstance(value, Mapping) else {}


def _parse_stored_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("stored timestamp is unavailable")
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("stored timestamp is naïve")
    return timestamp.astimezone(IST)


def _stored_quality_report(
    tradable: object, codes: object, details: Mapping[str, object], checked_at: datetime
) -> DataQualityReport:
    parsed_codes: list[DataQualityCode] = []
    if isinstance(codes, str):
        try:
            raw_codes: object = json.loads(codes)
        except json.JSONDecodeError:
            raw_codes = codes
    else:
        raw_codes = codes
    if isinstance(raw_codes, list):
        for value in raw_codes:
            try:
                parsed_codes.append(DataQualityCode(str(value)))
            except ValueError:
                parsed_codes.append(DataQualityCode.QUALITY_MISSING)
    elif raw_codes:
        parsed_codes.append(DataQualityCode.QUALITY_MISSING)
    normalized_details = {str(key): str(value) for key, value in details.items()}
    return DataQualityReport(
        tradable=bool(tradable),
        codes=tuple(dict.fromkeys(parsed_codes)),
        checked_at=checked_at,
        details=normalized_details,
    )


def _latest_json_evidence(root: Path, instrument: str) -> Mapping[str, object] | None:
    if not root.exists() or not root.is_dir():
        return None
    candidates: list[tuple[datetime, Mapping[str, object]]] = []
    for path in sorted(root.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        value = _normalise_evidence(value)
        if value is None or value.get("instrument") != instrument:
            continue
        timestamp_value = value.get("available_at", value.get("created_at", value.get("as_of")))
        try:
            timestamp = _parse_stored_timestamp(timestamp_value)
        except (TypeError, ValueError):
            continue
        candidates.append((timestamp, dict(value)))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _normalise_evidence(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    payload = value.get("payload")
    if isinstance(payload, Mapping):
        merged = dict(payload)
        for key in ("instrument", "available_at", "created_at", "as_of"):
            if key in value:
                merged.setdefault(key, value[key])
        return {str(key): item for key, item in merged.items()}
    return {str(key): item for key, item in value.items()}


def _latest_sqlite_evidence(
    database: Path, instrument: str, *, field: str | None
) -> Mapping[str, object] | None:
    if not database.exists():
        return None
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            "SELECT payload, created_at FROM research_signals ORDER BY created_at DESC, signal_id DESC"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    for payload, created_at in rows:
        try:
            parsed = _normalise_evidence(json.loads(payload) if isinstance(payload, str) else payload)
            timestamp = _parse_stored_timestamp(created_at)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if parsed is None or parsed.get("instrument") != instrument:
            continue
        if field is not None:
            selected = parsed.get(field)
            if not isinstance(selected, Mapping):
                continue
            return {"instrument": instrument, **dict(selected), "available_at": timestamp.isoformat()}
        return dict(parsed)
    return None


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


def _count(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return 0


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
        if isinstance(value, Mapping) and value.get("instrument") == instrument:
            reports.append(dict(value))
    return tuple(reports)


if __name__ == "__main__":  # pragma: no cover - Streamlit invokes the script.
    main()
