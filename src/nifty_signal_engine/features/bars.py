"""Coverage policy for sampled spot OHLC, not exchange trade OHLC.

A bar needs its first sample at the minute boundary, no gap over 15 seconds,
and a last sample no more than 15 seconds before close. No forward filling.
Flow totals include only intervals whose two endpoints fall within the bar;
cross-minute flow remains available in FLOW rows but is not misattributed.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class MinuteCoverage:
    start: datetime
    timestamps: list[datetime] = field(default_factory=list)
    valid: bool = True

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=1)

    def add(self, timestamp: datetime) -> None:
        if not self.timestamps and timestamp != self.start:
            self.valid = False
        if self.timestamps and not timedelta(0) < timestamp - self.timestamps[
            -1
        ] <= timedelta(seconds=15):
            self.valid = False
        self.timestamps.append(timestamp)

    def complete(self, watermark: datetime) -> bool:
        return bool(
            self.valid
            and self.timestamps
            and watermark >= self.end
            and self.end - self.timestamps[-1] <= timedelta(seconds=15)
        )
