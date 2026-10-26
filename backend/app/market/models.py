"""Data models for market data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds
    session_open: float | None = None  # First price seen since server start

    @property
    def session_change_percent(self) -> float:
        """Percentage change since the session open (0.0 if unknown)."""
        if not self.session_open:
            return 0.0
        return round((self.price - self.session_open) / self.session_open * 100, 4)

    @property
    def change(self) -> float:
        """Absolute price change from previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from previous update."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "session_open": self.session_open if self.session_open is not None else self.price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "session_change_percent": self.session_change_percent,
            "direction": self.direction,
        }
