"""Instrument-specific configuration kept outside calculation code."""

from typing import Literal

from pydantic import BaseModel

Instrument = Literal["NIFTY", "BANKNIFTY"]


class InstrumentConfig(BaseModel, frozen=True):
    """Static configuration for one supported index."""

    instrument: Instrument
    lot_size: int


INSTRUMENTS: dict[Instrument, InstrumentConfig] = {
    "NIFTY": InstrumentConfig(instrument="NIFTY", lot_size=75),
    "BANKNIFTY": InstrumentConfig(instrument="BANKNIFTY", lot_size=35),
}
