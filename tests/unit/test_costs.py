import pytest

from nifty_signal_engine.backtesting.costs import CostedTrade, CostRates, CostSchedule
from nifty_signal_engine.backtesting.fills import (
    ExecutableQuote,
    FillSimulator,
    TradeCandidate,
)
from tests.factories import aware


def test_cost_breakdown_names_every_configured_component() -> None:
    candidate = TradeCandidate(
        aware("2026-08-26T10:00:00+05:30"), "contract", quantity=10
    )
    fill = FillSimulator().enter_long(
        candidate,
        ExecutableQuote(aware("2026-08-26T10:00:15+05:30"), "contract", 99, 101),
    )
    rates = CostRates(
        brokerage_rate=0.001,
        exchange_charge_rate=0.002,
        transaction_tax_rate=0.003,
        gst_rate=0.18,
        regulatory_fee_rate=0.0001,
        stamp_duty_rate=0.0002,
        extra_slippage_bps=5,
    )

    breakdown = CostSchedule(version="fixture-v1", rates=rates).estimate(
        CostedTrade(entry=fill, quantity=10)
    )

    assert breakdown.version == "fixture-v1"
    assert breakdown.brokerage == pytest.approx(1.01)
    assert breakdown.exchange_charges == pytest.approx(2.02)
    assert breakdown.taxes == 0
    assert breakdown.gst == pytest.approx((1.01 + 2.02) * 0.18)
    assert breakdown.regulatory_fees == pytest.approx(0.101)
    assert breakdown.stamp_duty == pytest.approx(0.202)
    assert breakdown.spread == pytest.approx(10)
    assert breakdown.extra_slippage == pytest.approx(0.505)
    assert breakdown.total == pytest.approx(sum(breakdown.components.values()))


def test_schedule_report_exposes_the_version_and_all_rates() -> None:
    rates = CostRates(0, 0, 0, 0, 0, 0, 0)

    report = CostSchedule(version="zero-rates-for-test", rates=rates).report()

    assert report["version"] == "zero-rates-for-test"
    assert report["rates"] == rates.as_dict()
