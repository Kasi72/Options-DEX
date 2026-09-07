"""Contract tests for the Streamlit research-only interface."""

from pathlib import Path


def test_streamlit_layer_has_no_collection_or_calculation_dependencies() -> None:
    source = Path("src/nifty_signal_engine/streamlit_app.py").read_text()
    forbidden = ["requests.", "httpx.", "time.sleep", "brentq", "norm.cdf", "open("]
    assert not any(token in source for token in forbidden)


def test_dashboard_defaults_to_research_no_trade_without_promotion() -> None:
    from nifty_signal_engine.streamlit_app import research_action

    result = research_action(None)

    assert result["mode"] == "RESEARCH"
    assert result["action"] == "NO_TRADE"
    assert "MODEL_NOT_PROMOTED" in result["reasons"]


def test_precision_is_hidden_without_complete_governance_evidence() -> None:
    from nifty_signal_engine.streamlit_app import precision_display

    result = precision_display(
        {
            "precision": 0.75,
            "coverage": 0.2,
            "sample_count": 4,
            "precision_ci": (0.3, 0.95),
        }
    )

    assert result["precision"] is None
    assert result["reason"] == "INSUFFICIENT_EVIDENCE"

