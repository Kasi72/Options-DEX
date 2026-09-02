"""Explicit research-only collection commands with no import-time side effects."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from time import sleep
from typing import cast
from zoneinfo import ZoneInfo

from nifty_signal_engine.data.collector import CollectionResult, Collector, Instrument
from nifty_signal_engine.data.dhan_client import DhanClient, RawSnapshot
from nifty_signal_engine.data.repositories import SnapshotRepository
from nifty_signal_engine.monitoring.data_quality import TradingCalendar

IST = ZoneInfo("Asia/Kolkata")


class _FixtureBroker:
    """Fixture-only broker used for deterministic local smoke testing."""

    def __init__(self, raw: RawSnapshot) -> None:
        self._raw = raw

    async def fetch_expiries(self, _instrument: Instrument) -> tuple[date, ...]:
        assert self._raw.expiry is not None
        return (self._raw.expiry,)

    async def fetch_option_chain(
        self, _instrument: Instrument, _expiry: date
    ) -> RawSnapshot:
        return self._raw


def app() -> None:
    """Run the command-line interface only when explicitly invoked."""
    parser = _parser()
    arguments = parser.parse_args()
    if arguments.command == "inspect-session":
        _inspect_session(arguments)
        return
    if arguments.command in {"collect-once", "collect"}:
        _collect(arguments)
        return
    parser.error("a command is required")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nifty-signal")
    commands = parser.add_subparsers(dest="command")
    for command in ("collect-once", "collect"):
        collect = commands.add_parser(command)
        collect.add_argument(
            "--instrument", choices=("NIFTY", "BANKNIFTY"), required=True
        )
        collect.add_argument("--fixture", type=Path)
        collect.add_argument("--captured-at")
        collect.add_argument(
            "--data-dir", type=Path, default=Path(".nifty-signal-data")
        )
        if command == "collect":
            collect.add_argument("--count", type=int, default=1)
            collect.add_argument("--interval-seconds", type=float, default=60.0)
    inspect = commands.add_parser("inspect-session")
    inspect.add_argument("--instrument", choices=("NIFTY", "BANKNIFTY"), required=True)
    inspect.add_argument("--session-date", required=True)
    inspect.add_argument("--data-dir", type=Path, default=Path(".nifty-signal-data"))
    return parser


def _collect(arguments: argparse.Namespace) -> None:
    data_dir = cast(Path, arguments.data_dir)
    instrument = cast(Instrument, arguments.instrument)
    broker, calendar, fixture_clock = _broker_from_arguments(arguments)
    count = 1 if arguments.command == "collect-once" else int(arguments.count)
    interval = (
        0.0
        if arguments.command == "collect-once"
        else float(arguments.interval_seconds)
    )
    if count < 1 or interval < 0:
        raise SystemExit(
            "count must be at least one and interval-seconds cannot be negative"
        )
    with SnapshotRepository(
        database_path=data_dir / "market.sqlite3", parquet_root=data_dir / "parquet"
    ) as repository:
        collector = Collector(
            broker=broker,
            repository=repository,
            calendar=calendar,
            clock=fixture_clock,
        )
        for index in range(count):
            result = asyncio.run(collector.collect_once(instrument))
            print(json.dumps(_result_payload(result), sort_keys=True))
            if result.status != "COLLECTED":
                raise SystemExit(1)
            if index + 1 < count:
                sleep(interval)


def _broker_from_arguments(
    arguments: argparse.Namespace,
) -> tuple[
    DhanClient | _FixtureBroker, TradingCalendar | None, Callable[[], datetime] | None
]:
    fixture = cast(Path | None, arguments.fixture)
    if fixture is not None:
        payload = _fixture_payload(fixture)
        metadata = payload.get("fixture_metadata")
        if not isinstance(metadata, dict):
            raise SystemExit(
                "fixture_metadata is required for deterministic fixture collection"
            )
        captured_value = arguments.captured_at or metadata.get("captured_at")
        source_value = metadata.get("broker_source_timestamp")
        expiry_value = metadata.get("expiry")
        trading_dates = metadata.get("trading_dates")
        if (
            not isinstance(captured_value, str)
            or not isinstance(source_value, str)
            or not isinstance(expiry_value, str)
            or not isinstance(trading_dates, list)
            or not all(isinstance(value, str) for value in trading_dates)
        ):
            raise SystemExit(
                "fixture_metadata has invalid capture/source/expiry/calendar values"
            )
        try:
            expiry = date.fromisoformat(expiry_value)
            calendar = TradingCalendar(
                {date.fromisoformat(value) for value in trading_dates}
            )
        except ValueError as error:
            raise SystemExit("fixture_metadata contains an invalid ISO date") from error
        raw = RawSnapshot(
            body=fixture.read_bytes(),
            captured_at=_parse_timestamp(captured_value),
            expiry=expiry,
            broker_source_timestamp=_parse_timestamp(source_value),
            source_time_authoritative=metadata.get("source_time_authoritative") is True,
        )
        return _FixtureBroker(raw), calendar, lambda: raw.captured_at
    token = os.environ.get("DHAN_ACCESS_TOKEN")
    client_id = os.environ.get("DHAN_CLIENT_ID")
    if not token or not client_id:
        raise SystemExit(
            "DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID are required for live collection"
        )
    return DhanClient(access_token=token, client_id=client_id), None, None


def _fixture_payload(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit("fixture must contain valid JSON metadata") from error
    if not isinstance(payload, dict):
        raise SystemExit("fixture must contain a JSON object")
    return payload


def _parse_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None or timestamp.tzinfo.utcoffset(timestamp) is None:
        raise SystemExit("captured-at must be timezone-aware")
    return timestamp.astimezone(IST)


def _result_payload(result: CollectionResult) -> dict[str, object]:
    quality = result.quality
    return {
        "instrument": result.instrument,
        "status": result.status,
        "raw_snapshot_id": result.raw_snapshot_id,
        "research_only": True,
        "tradable": quality.tradable if quality is not None else False,
        "quality_codes": [str(code) for code in quality.codes]
        if quality is not None
        else [],
        "error_type": result.error_type,
        "active_expiries": [expiry.isoformat() for expiry in result.active_expiries],
        "selected_expiry": (
            result.selected_expiry.isoformat()
            if result.selected_expiry is not None
            else None
        ),
    }


def _inspect_session(arguments: argparse.Namespace) -> None:
    data_dir = cast(Path, arguments.data_dir)
    instrument = cast(Instrument, arguments.instrument)
    try:
        selected_date = date.fromisoformat(cast(str, arguments.session_date))
    except ValueError as error:
        raise SystemExit("session-date must be ISO YYYY-MM-DD") from error
    with SnapshotRepository(
        database_path=data_dir / "market.sqlite3", parquet_root=data_dir / "parquet"
    ) as repository:
        summary = repository.session_quality_summary(instrument, selected_date)
    print(
        json.dumps(
            {
                "instrument": instrument,
                "session_date": selected_date.isoformat(),
                **summary,
            },
            sort_keys=True,
        )
    )


def main(_arguments: Sequence[str] | None = None) -> None:
    """Compatibility helper for direct `python -m` invocation."""
    app()


if __name__ == "__main__":
    main()
