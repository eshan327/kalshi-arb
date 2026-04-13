from __future__ import annotations

from dataclasses import dataclass

from core.market_profiles import MarketProfile
from feeds.brti_calc import calculate_brti, reset_brti_calc_state
from feeds.state.book_store import ExchangeBook


@dataclass
class RTIPipeline:
    profile: MarketProfile

    def reset(self) -> None:
        reset_brti_calc_state()

    def calculate(
        self,
        exchange_books: dict[str, ExchangeBook],
        now_ts: float,
    ) -> tuple[float | None, int, int]:
        return calculate_brti(
            exchange_books,
            now_ts,
            spacing=self.profile.index_spacing_units,
            deviation_threshold=self.profile.index_deviation_threshold,
            potentially_erroneous_param=self.profile.index_erroneous_threshold,
            stale_threshold=self.profile.index_stale_threshold_sec,
        )
