"""Versioned, fully disclosed transaction-cost accounting."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType

from nifty_signal_engine.backtesting.fills import Fill


def _rate(value: float, field: str) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return float(value)


@dataclass(frozen=True, slots=True)
class CostRates:
    """All rates used by a schedule; no live rate is embedded in code."""

    brokerage_rate: float
    exchange_charge_rate: float
    transaction_tax_rate: float
    gst_rate: float
    regulatory_fee_rate: float
    stamp_duty_rate: float
    extra_slippage_bps: float

    def __post_init__(self) -> None:
        for field in (
            "brokerage_rate",
            "exchange_charge_rate",
            "transaction_tax_rate",
            "gst_rate",
            "regulatory_fee_rate",
            "stamp_duty_rate",
            "extra_slippage_bps",
        ):
            object.__setattr__(self, field, _rate(getattr(self, field), field))

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CostedTrade:
    """One long option position, optionally closed by a later fill."""

    entry: Fill
    exit: Fill | None = None
    quantity: int | None = None

    def __post_init__(self) -> None:
        quantity = self.entry.quantity if self.quantity is None else self.quantity
        if isinstance(quantity, bool) or quantity <= 0:
            raise ValueError("quantity must be positive")
        if quantity != self.entry.quantity:
            raise ValueError("cost trade quantity must equal entry quantity")
        if self.exit is not None:
            if self.exit.candidate.contract_id != self.entry.candidate.contract_id:
                raise ValueError("entry and exit contracts must match")
            if self.exit.executed_at <= self.entry.executed_at:
                raise ValueError("exit must follow entry")
            if self.exit.quantity != quantity:
                raise ValueError("exit quantity must equal entry quantity")
        object.__setattr__(self, "quantity", quantity)

    @property
    def entry_notional(self) -> float:
        quantity = self.quantity
        assert quantity is not None
        return self.entry.price * quantity

    @property
    def exit_notional(self) -> float:
        if self.exit is None:
            return 0.0
        quantity = self.quantity
        assert quantity is not None
        return self.exit.price * quantity

    @property
    def turnover(self) -> float:
        return self.entry_notional + self.exit_notional


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    version: str
    brokerage: float
    exchange_charges: float
    taxes: float
    gst: float
    regulatory_fees: float
    stamp_duty: float
    spread: float
    extra_slippage: float

    @property
    def components(self) -> Mapping[str, float]:
        return MappingProxyType(
            {
                "brokerage": self.brokerage,
                "exchange_charges": self.exchange_charges,
                "taxes": self.taxes,
                "gst": self.gst,
                "regulatory_fees": self.regulatory_fees,
                "stamp_duty": self.stamp_duty,
                "spread": self.spread,
                "extra_slippage": self.extra_slippage,
            }
        )

    @property
    def total(self) -> float:
        return sum(self.components.values())


@dataclass(frozen=True, slots=True)
class CostSchedule:
    """A versioned rate card that can be persisted alongside every replay."""

    version: str
    rates: CostRates

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("cost schedule version must not be blank")
        if not isinstance(self.rates, CostRates):
            raise TypeError("rates must be CostRates")

    def estimate(self, trade: CostedTrade) -> CostBreakdown:
        if not isinstance(trade, CostedTrade):
            raise TypeError("trade must be CostedTrade")
        turnover = trade.turnover
        brokerage = turnover * self.rates.brokerage_rate
        exchange = turnover * self.rates.exchange_charge_rate
        # Transaction tax is charged on the sale leg.  An unclosed entry has no
        # inferred future sale, so the estimate keeps it at zero rather than
        # inventing a close price.
        taxes = trade.exit_notional * self.rates.transaction_tax_rate
        gst = (brokerage + exchange) * self.rates.gst_rate
        regulatory = turnover * self.rates.regulatory_fee_rate
        stamp_duty = trade.entry_notional * self.rates.stamp_duty_rate
        quantity = trade.quantity
        assert quantity is not None
        spread = (trade.entry.ask - trade.entry.bid) * quantity / 2
        if trade.exit is not None:
            spread += (trade.exit.ask - trade.exit.bid) * quantity / 2
        extra_slippage = turnover * self.rates.extra_slippage_bps / 10_000
        return CostBreakdown(
            version=self.version,
            brokerage=brokerage,
            exchange_charges=exchange,
            taxes=taxes,
            gst=gst,
            regulatory_fees=regulatory,
            stamp_duty=stamp_duty,
            spread=spread,
            extra_slippage=extra_slippage,
        )

    def report(self) -> Mapping[str, object]:
        """Serializable schedule metadata for every replay report."""
        return MappingProxyType(
            {"version": self.version, "rates": self.rates.as_dict()}
        )
