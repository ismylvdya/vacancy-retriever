from __future__ import annotations

from ..config import ModelPrice


def cost_usd(price: ModelPrice, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> float:
    per_token = 1_000_000
    return (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read * price.cache_read
        + cache_write * price.cache_write
    ) / per_token
