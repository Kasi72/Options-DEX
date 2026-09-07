"""Run the Dhan collector locally during NSE market hours.

This is the no-cost alternative to an always-on cloud worker. Keep this
process running on a powered-on computer; it writes snapshots to Supabase and
does not submit orders.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as clock_time
from zoneinfo import ZoneInfo

from collect_to_supabase import collect

IST = ZoneInfo("Asia/Kolkata")
OPEN = clock_time(9, 15)
CLOSE = clock_time(15, 30)
INTERVAL_SECONDS = 60
LOG = logging.getLogger("nifty-collector")


def market_open(now: datetime) -> bool:
    """Return whether the local NSE session is open on a weekday."""
    local = now.astimezone(IST)
    return local.weekday() < 5 and OPEN <= local.time() < CLOSE


def seconds_to_next_minute(now: datetime) -> float:
    """Sleep until the next minute boundary, avoiding drift over the day."""
    return max(1.0, 60 - now.second - now.microsecond / 1_000_000)


async def run_forever() -> None:
    LOG.info("local collector started; polling every %ss during NSE hours", INTERVAL_SECONDS)
    while True:
        now = datetime.now(IST)
        if market_open(now):
            for instrument in ("NIFTY", "BANKNIFTY"):
                try:
                    await collect(instrument)  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001 - keep the scheduler alive
                    LOG.exception("collection failed for %s; retrying next cycle", instrument)
            await asyncio.sleep(seconds_to_next_minute(datetime.now(IST)))
        else:
            # Check once per minute outside session; no API calls are made.
            await asyncio.sleep(INTERVAL_SECONDS)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        LOG.info("local collector stopped")


if __name__ == "__main__":
    main()
