"""Deterministic as-of features. Models consume only ``drain_completed``.

``on_snapshot`` emits FLOW rows from the second valid snapshot. Availability is
the original audit's checked_at, never the exchange clock or replay wall clock.
Source authority must already be established upstream; no timestamp is promoted.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Literal

from nifty_signal_engine.config.instruments import INSTRUMENTS, InstrumentConfig
from nifty_signal_engine.config.market_hours import (
    IST,
    REGULAR_OPEN,
    MarketPhase,
    market_phase,
)
from nifty_signal_engine.domain.market import OptionChainSnapshot
from nifty_signal_engine.features.bars import MinuteCoverage
from nifty_signal_engine.features.flow import flow_values, incremental_weights
from nifty_signal_engine.features.option_structure import FeatureValue, structure_values
from nifty_signal_engine.features.price import price_values
from nifty_signal_engine.monitoring.data_quality import (
    MAX_SOURCE_AGE,
    DataQualityReport,
)


def _ist(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(IST)


@dataclass(frozen=True)
class FeatureRow:
    available_at: datetime
    instrument: str
    session_date: date
    schema_version: str
    values: Mapping[str, FeatureValue]
    quality_codes: tuple[str, ...]
    source_timestamps: tuple[datetime, ...] = ()
    row_kind: Literal["FLOW", "COMPLETED_MINUTE"] = "FLOW"
    completed: bool = False
    bar_start: datetime | None = None
    bar_end: datetime | None = None
    flow_start: datetime | None = None

    def __post_init__(self) -> None:
        available = _ist(self.available_at)
        timestamps = tuple(sorted({_ist(t) for t in self.source_timestamps}))
        if any(t > available for t in timestamps):
            raise ValueError("future source timestamp")
        if available.date() != self.session_date:
            raise ValueError("availability must belong to session")
        if self.completed != (self.row_kind == "COMPLETED_MINUTE"):
            raise ValueError("row kind/completion mismatch")
        if self.completed and (self.bar_start is None or self.bar_end is None):
            raise ValueError("completed row requires bar boundaries")
        if (
            self.completed
            and self.bar_start is not None
            and self.bar_end is not None
            and (
                _ist(self.bar_end) - _ist(self.bar_start) != timedelta(minutes=1)
                or self.bar_start.second
                or self.bar_start.microsecond
            )
        ):
            raise ValueError("invalid one-minute bar boundaries")
        for boundary in (self.bar_start, self.bar_end, self.flow_start):
            if boundary is not None and _ist(boundary) > available:
                raise ValueError("future bar boundary")
        if any(
            not isinstance(v, (str, int, float, bool, type(None)))
            or (isinstance(v, float) and not math.isfinite(v))
            for v in self.values.values()
        ):
            raise ValueError("features must be finite scalar values")
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "source_timestamps", timestamps)
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "quality_codes", tuple(self.quality_codes))
        for name in ("bar_start", "bar_end", "flow_start"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _ist(value))

    def to_json_bytes(self) -> bytes:
        """Canonical UTF-8 serialization independent of mapping insertion order."""
        return json.dumps(
            {
                "available_at": self.available_at.isoformat(),
                "instrument": self.instrument,
                "session_date": self.session_date.isoformat(),
                "schema_version": self.schema_version,
                "values": dict(self.values),
                "quality_codes": self.quality_codes,
                "source_timestamps": [t.isoformat() for t in self.source_timestamps],
                "row_kind": self.row_kind,
                "completed": self.completed,
                "bar_start": self.bar_start.isoformat() if self.bar_start else None,
                "bar_end": self.bar_end.isoformat() if self.bar_end else None,
                "flow_start": self.flow_start.isoformat() if self.flow_start else None,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()


@dataclass
class _State:
    previous: OptionChainSnapshot
    checked_at: datetime
    session_open: float
    bar: MinuteCoverage
    spots: list[float] = field(default_factory=list)
    rows: list[FeatureRow] = field(default_factory=list)
    ends: list[datetime] = field(default_factory=list)
    closes: list[float] = field(default_factory=list)
    ranges: list[tuple[float, float]] = field(default_factory=list)
    previous_structure: dict[str, FeatureValue] | None = None
    completed_structure: dict[str, FeatureValue] | None = None


class FeaturePipeline:
    """One isolated state per instrument, expiry and IST session.

    A rejected event invalidates active bar coverage but does not overwrite the
    last valid counter baseline. ``last_rejection_codes`` explains None returns;
    first accepted baselines are reported as WARMUP. Gaps reset price windows.
    """

    def __init__(
        self,
        *,
        configs: Mapping[str, InstrumentConfig] | None = None,
        risk_free_rate: float = 0.0,
    ) -> None:
        selected = dict(INSTRUMENTS if configs is None else configs)
        if not math.isfinite(risk_free_rate):
            raise ValueError("rate must be finite")
        if any(k != c.instrument or c.lot_size <= 0 for k, c in selected.items()):
            raise ValueError("invalid instrument config")
        self._configs = selected
        self._rate = risk_free_rate
        self._states: dict[tuple[str, date, date], _State] = {}
        self._completed: list[FeatureRow] = []
        self.last_rejection_codes: tuple[str, ...] = ()

    def drain_completed(self) -> tuple[FeatureRow, ...]:
        result = tuple(self._completed)
        self._completed.clear()
        return result

    def on_snapshot(
        self, snapshot: OptionChainSnapshot, quality: DataQualityReport | None
    ) -> FeatureRow | None:
        # Observation audits are not historical market inputs. Ignoring them also
        # preserves online/replay parity when a repeated poll is audited separately.
        if quality is not None and quality.details.get("decision_scope") is not None:
            self.last_rejection_codes = ("OBSERVATION_AUDIT_NOT_HISTORICAL",)
            return None
        timestamp = _ist(snapshot.source_timestamp)
        key = (snapshot.instrument, snapshot.expiry, timestamp.date())
        state = self._states.get(key)
        try:
            checked = self._validate(snapshot, quality)
            if state is not None:
                if (
                    timestamp <= state.previous.source_timestamp
                    or checked < state.checked_at
                ):
                    raise ValueError("OUT_OF_ORDER")
                weights = incremental_weights(snapshot, state.previous)
            else:
                weights = tuple(0 for _ in snapshot.quotes)
            values = structure_values(
                snapshot, self._configs[snapshot.instrument], weights, self._rate
            )
        except (ValueError, KeyError, OverflowError) as exc:
            self.last_rejection_codes = (str(exc),)
            if state is not None:
                state.bar.valid = False
            return None

        self.last_rejection_codes = ()
        minute = timestamp.replace(second=0, microsecond=0)
        if state is None:
            state = _State(snapshot, checked, snapshot.spot, MinuteCoverage(minute))
            state.bar.add(timestamp)
            state.spots.append(snapshot.spot)
            state.previous_structure = values
            self._states[key] = state
            self.last_rejection_codes = ("WARMUP",)
            return None

        if minute != state.bar.start:
            self._finish(state, checked)
            state.bar = MinuteCoverage(minute)
            state.spots = []
            state.rows = []
        values.update(flow_values(snapshot, weights))
        values.update(
            price_values([snapshot.spot], (), (), timestamp, state.session_open, ())
        )
        values["minutes_since_open"] = (
            timestamp - datetime.combine(timestamp.date(), REGULAR_OPEN, IST)
        ).total_seconds() / 60
        prior = state.previous_structure or {}
        for name in ("atm_iv", "atm_skew", "atm_straddle"):
            before, current = prior.get(name), values[name]
            values[f"{name}_change"] = (
                float(current) - float(before)
                if isinstance(before, (int, float))
                and isinstance(current, (int, float))
                else None
            )
        sources = (
            timestamp,
            snapshot.received_at,
            checked,
            state.previous.source_timestamp,
            state.previous.received_at,
            state.checked_at,
            *(q.timestamp for q in snapshot.quotes),
            *(q.timestamp for q in state.previous.quotes),
        )
        row = FeatureRow(
            available_at=checked,
            instrument=snapshot.instrument,
            session_date=timestamp.date(),
            schema_version="1",
            values=values,
            quality_codes=(),
            source_timestamps=sources,
            flow_start=state.previous.source_timestamp,
        )
        state.bar.add(timestamp)
        state.spots.append(snapshot.spot)
        state.rows.append(row)
        state.previous = snapshot
        state.checked_at = checked
        state.previous_structure = values
        return row

    def _finish(self, state: _State, checked: datetime) -> None:
        if state.ends and state.ends[-1] != state.bar.start:
            state.ends.clear()
            state.closes.clear()
            state.ranges.clear()
            state.completed_structure = None
        if not state.bar.complete(checked) or not state.rows:
            state.ends.clear()
            state.closes.clear()
            state.ranges.clear()
            state.completed_structure = None
            return
        last = state.rows[-1]
        values = dict(last.values)
        values.update(
            price_values(
                state.spots,
                state.ends,
                state.closes,
                state.bar.end,
                state.session_open,
                state.ranges,
            )
        )
        for name in ("atm_iv", "atm_skew", "atm_straddle"):
            before = (state.completed_structure or {}).get(name)
            current = values[name]
            values[f"{name}_change"] = (
                float(current) - float(before)
                if isinstance(before, (int, float))
                and isinstance(current, (int, float))
                else None
            )
        # Only within-minute endpoint pairs; boundary samples baseline the next minute.
        included = [
            r
            for r in state.rows
            if r.flow_start is not None and r.flow_start >= state.bar.start
        ]
        for name in (
            "call_volume_increment",
            "put_volume_increment",
            "flow_gex_rupees",
            "flow_gex_crore",
            "flow_dex_rupees",
            "flow_dex_crore",
        ):
            total = 0.0
            for row in included:
                value = row.values[name]
                assert isinstance(value, (int, float))
                total += value
            values[name] = total
        sources = tuple(t for row in state.rows for t in row.source_timestamps)
        self._completed.append(
            FeatureRow(
                available_at=checked,
                instrument=last.instrument,
                session_date=last.session_date,
                schema_version=last.schema_version,
                values=values,
                quality_codes=last.quality_codes,
                source_timestamps=(*sources, checked),
                row_kind="COMPLETED_MINUTE",
                completed=True,
                bar_start=state.bar.start,
                bar_end=state.bar.end,
            )
        )
        state.ends.append(state.bar.end)
        state.closes.append(state.spots[-1])
        state.ranges.append((max(state.spots), min(state.spots)))
        state.completed_structure = values

    def _validate(
        self, snapshot: OptionChainSnapshot, quality: DataQualityReport | None
    ) -> datetime:
        if quality is None:
            raise ValueError("QUALITY_MISSING")
        checked = _ist(quality.checked_at)
        source, receipt = _ist(snapshot.source_timestamp), _ist(snapshot.received_at)
        if not quality.tradable or quality.codes:
            raise ValueError(",".join(str(c) for c in quality.codes) or "NON_TRADABLE")
        if quality.details.get("decision_scope") is not None:
            raise ValueError("OBSERVATION_AUDIT_NOT_HISTORICAL")
        if not snapshot.source_time_authoritative or any(
            not q.timestamp_authoritative for q in snapshot.quotes
        ):
            raise ValueError("UNTRUSTED_TIMESTAMP")
        if (
            source > receipt
            or receipt > checked
            or any(_ist(q.timestamp) > source for q in snapshot.quotes)
        ):
            raise ValueError("FUTURE_INPUT_TIMESTAMP")
        if checked - source > MAX_SOURCE_AGE or any(
            checked - _ist(q.timestamp) > MAX_SOURCE_AGE for q in snapshot.quotes
        ):
            raise ValueError("STALE_INPUT")
        if (
            source.date() != checked.date()
            or market_phase(source) != MarketPhase.REGULAR
        ):
            raise ValueError("OUTSIDE_SESSION")
        if (
            not math.isfinite(snapshot.spot)
            or snapshot.spot <= 0
            or not snapshot.quotes
        ):
            raise ValueError("INVALID_SPOT_OR_EMPTY_CHAIN")
        contracts = {(q.strike, q.option_type) for q in snapshot.quotes}
        if len(contracts) != len(snapshot.quotes) or any(
            (strike, side) not in contracts
            for strike, _ in contracts
            for side in ("CE", "PE")
        ):
            raise ValueError("INCOMPLETE_CONTRACTS")
        for q in snapshot.quotes:
            if (
                q.expiry != snapshot.expiry
                or q.volume < 0
                or q.oi < 0
                or not math.isfinite(q.strike)
                or q.strike <= 0
                or q.iv is None
                or not math.isfinite(q.iv)
                or not 0 < q.iv <= 1
                or (q.ltp is not None and (not math.isfinite(q.ltp) or q.ltp < 0))
            ):
                raise ValueError("INVALID_QUOTE")
        return checked
