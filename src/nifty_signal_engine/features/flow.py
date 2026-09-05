"""Counter deltas require an unchanged, complete contract universe."""

from nifty_signal_engine.domain.market import OptionChainSnapshot

FLOW_FIELDS = (
    "call_volume_increment",
    "put_volume_increment",
    "flow_gex_rupees",
    "flow_gex_crore",
    "flow_dex_rupees",
    "flow_dex_crore",
)

ROLLING_NAMES = {
    "call_volume_increment": "call_volume_increment_5m",
    "put_volume_increment": "put_volume_increment_5m",
    "flow_gex_rupees": "flow_gex_rupees_5m",
    "flow_gex_crore": "flow_gex_crore_5m",
    "flow_dex_rupees": "flow_dex_rupees_5m",
    "flow_dex_crore": "flow_dex_crore_5m",
}


def _acceleration_name(field: str, horizon: str) -> str:
    if field.startswith(("flow_gex_", "flow_dex_")):
        family, unit = field.rsplit("_", 1)
        return f"{family}_acceleration_{horizon}_{unit}"
    return field.replace("_increment", f"_acceleration_{horizon}")


def incremental_weights(
    current: OptionChainSnapshot, previous: OptionChainSnapshot
) -> tuple[int, ...]:
    before = {(q.expiry, q.strike, q.option_type): q.volume for q in previous.quotes}
    keys = [(q.expiry, q.strike, q.option_type) for q in current.quotes]
    if len(set(keys)) != len(keys) or set(keys) != set(before):
        raise ValueError("CONTRACT_UNIVERSE_CHANGED")
    result = tuple(
        q.volume - before[key] for q, key in zip(current.quotes, keys, strict=True)
    )
    if any(value < 0 for value in result):
        raise ValueError("COUNTER_RESET")
    return result


def flow_values(
    snapshot: OptionChainSnapshot, weights: tuple[int, ...]
) -> dict[str, float | str | None]:
    values: dict[str, float | str | None] = {
        "call_volume_increment": sum(
            w
            for q, w in zip(snapshot.quotes, weights, strict=True)
            if q.option_type == "CE"
        ),
        "put_volume_increment": sum(
            w
            for q, w in zip(snapshot.quotes, weights, strict=True)
            if q.option_type == "PE"
        ),
    }
    for name in ROLLING_NAMES.values():
        values[name] = None
    for horizon in ("1m", "5m"):
        for field in FLOW_FIELDS:
            values[_acceleration_name(field, horizon)] = None
    values.update(
        {
            "flow_5m_status": "COMPLETED_BARS_ONLY",
            "flow_acceleration_1m_status": "COMPLETED_BARS_ONLY",
            "flow_acceleration_5m_status": "COMPLETED_BARS_ONLY",
        }
    )
    return values


def completed_flow_values(
    current: dict[str, float], history: list[dict[str, float]]
) -> dict[str, float | str | None]:
    """Derive causal windows from prior completed bars plus the current bar."""
    values: dict[str, float | str | None] = {}
    current_window = [*history[-4:], current]
    rolling_valid = len(current_window) == 5
    for field, output in ROLLING_NAMES.items():
        values[output] = (
            sum(item[field] for item in current_window) if rolling_valid else None
        )
    values["flow_5m_status"] = "VALID" if rolling_valid else "WARMUP"

    prior = history[-1] if history else None
    for field in FLOW_FIELDS:
        values[_acceleration_name(field, "1m")] = (
            current[field] - prior[field] if prior is not None else None
        )
    values["flow_acceleration_1m_status"] = "VALID" if prior is not None else "WARMUP"

    prior_window = history[-5:]
    acceleration_valid = len(prior_window) == 5
    for field, rolling_name in ROLLING_NAMES.items():
        rolling = values[rolling_name]
        values[_acceleration_name(field, "5m")] = (
            float(rolling) - sum(item[field] for item in prior_window)
            if acceleration_valid and rolling is not None
            else None
        )
    values["flow_acceleration_5m_status"] = "VALID" if acceleration_valid else "WARMUP"
    return values
