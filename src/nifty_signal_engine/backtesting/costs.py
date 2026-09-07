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
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
        ):
            raise ValueError("quantity must be a positive integer")
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
    brokerage_rupees: float
    exchange_charges_rupees: float
    taxes_rupees: float
    gst_rupees: float
    regulatory_fees_rupees: float
    stamp_duty_rupees: float
    spread_rupees: float
    extra_slippage_rupees: float
    currency: str = "INR"

    def __post_init__(self) -> None:
        if self.currency != "INR":
            raise ValueError("cost currency must be INR")
        for field in self.components:
            value = getattr(self, field)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{field} must be finite and non-negative")

    @property
    def components(self) -> Mapping[str, float]:
        return MappingProxyType(
            {
                "brokerage_rupees": self.brokerage_rupees,
                "exchange_charges_rupees": self.exchange_charges_rupees,
                "taxes_rupees": self.taxes_rupees,
                "gst_rupees": self.gst_rupees,
                "regulatory_fees_rupees": self.regulatory_fees_rupees,
                "stamp_duty_rupees": self.stamp_duty_rupees,
                "spread_rupees": self.spread_rupees,
                "extra_slippage_rupees": self.extra_slippage_rupees,
            }
        )

    @property
    def total_rupees(self) -> float:
        return sum(self.components.values())

    # Compatibility aliases retain the original terse read API. New reports and
    # component maps always use explicit INR/rupee field names.
    @property
    def brokerage(self) -> float:
        return self.brokerage_rupees

    @property
    def exchange_charges(self) -> float:
        return self.exchange_charges_rupees

    @property
    def taxes(self) -> float:
        return self.taxes_rupees

    @property
    def gst(self) -> float:
        return self.gst_rupees

    @property
    def regulatory_fees(self) -> float:
        return self.regulatory_fees_rupees

    @property
    def stamp_duty(self) -> float:
        return self.stamp_duty_rupees

    @property
    def spread(self) -> float:
        return self.spread_rupees

    @property
    def extra_slippage(self) -> float:
        return self.extra_slippage_rupees

    @property
    def total(self) -> float:
        return self.total_rupees


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
            brokerage_rupees=brokerage,
            exchange_charges_rupees=exchange,
            taxes_rupees=taxes,
            gst_rupees=gst,
            regulatory_fees_rupees=regulatory,
            stamp_duty_rupees=stamp_duty,
            spread_rupees=spread,
            extra_slippage_rupees=extra_slippage,
        )

    def report(self) -> Mapping[str, object]:
        """Serializable schedule metadata for every replay report."""
        return MappingProxyType(
            {
                "version": self.version,
                "currency": "INR",
                "monetary_unit": "rupees",
                "rates": self.rates.as_dict(),
            }
        )
