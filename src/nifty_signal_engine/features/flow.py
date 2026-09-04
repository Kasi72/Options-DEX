"""Counter deltas require an unchanged, complete contract universe."""

from nifty_signal_engine.domain.market import OptionChainSnapshot


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
) -> dict[str, float]:
    return {
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
