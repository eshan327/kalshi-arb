import time
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

@dataclass
class TwapCalculator:

    strike_price: float
    total_seconds: int = 60
    _prices: list[tuple[float, float]] = field(default_factory=list, repr=False)
    _window_start: float | None = field(default=None, repr=False)
    _last_known_price: float | None = field(default=None, repr=False)

    def start_window(self):
        """Triggers when the 60s settlement window begins."""

        self._window_start = time.time()
        self._prices.clear()
        logger.info(f"TWAP {self.total_seconds}s settlement window started for strike {self.strike_price}")

    def add_price_tick(self, price: float):
        """
        Records incoming spot price from WS; forward-fill any missing ticks.
        """

        self._last_known_price = price
        if self._window_start is not None and self.seconds_elapsed() <= self.total_seconds:
            self._prices.append((time.time(), price))

    def seconds_elapsed(self) -> int:
        """How many seconds have passed since the window started?"""

        if self._window_start is None:
            return 0
        return min(int(time.time() - self._window_start), self.total_seconds)

    def _get_discrete_samples(self) -> list[float]:
        """
        Reconstructs Kalshi's discrete 1-second sampling array.
        """

        if self._window_start is None or not self._prices:
            return []
            
        samples = []
        elapsed = self.seconds_elapsed()

        # One forward pass avoids rescanning historical ticks for each second.
        idx = 0
        valid_price = self._prices[0][1]
        price_count = len(self._prices)

        for sec in range(1, elapsed + 1):
            target_time = self._window_start + sec
            while idx < price_count and self._prices[idx][0] <= target_time:
                valid_price = self._prices[idx][1]
                idx += 1

            samples.append(valid_price)
            
        return samples

    def current_average(self) -> float | None:
        """Resolution math."""

        samples = self._get_discrete_samples()
        if not samples:
            return self._last_known_price # Fallback if haven't crossed 1s yet
        return sum(samples) / len(samples)

    def required_average(self) -> float | None:
        """The needed average left to hit the strike price."""

        elapsed = self.seconds_elapsed()
        if elapsed >= self.total_seconds or self._window_start is None:
            return None

        rem_seconds = self.total_seconds - elapsed
        current_sum = sum(self._get_discrete_samples())
        target_sum = self.strike_price * self.total_seconds
        
        return (target_sum - current_sum) / rem_seconds