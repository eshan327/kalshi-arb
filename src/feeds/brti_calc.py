import math
import statistics
import time

from feeds.state.book_store import ExchangeBook

# --- Index Methodology Parameters (from CME CF RTI methodology family) ---
# Section 6.2 style depth-walk parameters; applied to the active profile's spot orderbooks.
SPACING = 1.0
DEVIATION_THRESHOLD = 0.005  # D = 0.5%
POTENTIALLY_ERRONEOUS_PARAM = 0.05  # 5%
STALE_THRESHOLD = 30  # Discard exchange if data >30s old

# Tracks exchanges flagged as potentially erroneous (Section 5.3 step 4 hysteresis)
_flagged_exchanges: set[str] = set()


ExchangeBooks = dict[str, ExchangeBook]
Levels = list[tuple[float, float]]


def reset_brti_calc_state() -> None:
    """Clears cross-run exchange-screening hysteresis."""
    _flagged_exchanges.clear()


# ---- Section 4.1.3: Dynamic Order Size Cap (Eq. 4a-5) ----


def compute_dynamic_order_cap(
    uncapped_bids: Levels, uncapped_asks: Levels
) -> float | None:
    """
    Calculates the dynamic order size cap from the uncapped consolidated orderbook.
    Returns C_T = trimmed_mean + 5 * winsorized_std_dev (Eq. 5)
    """
    if not uncapped_bids or not uncapped_asks:
        return None

    # Best bid (highest) and best ask (lowest) from uncapped consolidated book
    best_bid = uncapped_bids[0][0]  # bids sorted descending
    best_ask = uncapped_asks[0][0]  # asks sorted ascending

    # Eq 4a: ask samples within 5% of best ask, up to 50
    ask_samples = []
    for price, size in uncapped_asks:
        if price <= 1.05 * best_ask:
            ask_samples.append(size)
        if len(ask_samples) >= 50:
            break

    # Eq 4b: bid samples within 5% of best bid, up to 50
    bid_samples = []
    for price, size in uncapped_bids:
        if price >= 0.95 * best_bid:
            bid_samples.append(size)
        if len(bid_samples) >= 50:
            break

    # Eq 4c: combine and sort ascending
    s_t = sorted(bid_samples + ask_samples)
    n_t = len(s_t)

    if not s_t:
        return None

    # Eq 4d: trimming size
    k = int(0.01 * n_t)

    # Eq 4e: trimmed mean
    if n_t - 2 * k <= 0:
        trimmed_mean = sum(s_t) / n_t
    else:
        trimmed_mean = sum(s_t[k : n_t - k]) / (n_t - 2 * k)

    # Eq 4f: winsorized sample set
    s_prime = []
    for i in range(n_t):
        if i < k:
            s_prime.append(
                s_t[k]
            )  # replace low outliers with s_{k+1} (0-indexed: s_t[k])
        elif i >= n_t - k:
            s_prime.append(
                s_t[n_t - k - 1]
            )  # replace high outliers with s_{n-k} (0-indexed: s_t[n_t-k-1])
        else:
            s_prime.append(s_t[i])

    # Eq 4h: winsorized sample standard deviation
    sigma = statistics.stdev(s_prime) if n_t > 1 else 0

    # Eq 5: C_T = trimmed_mean + 5 * sigma
    return trimmed_mean + 5 * sigma


# ---- Section 5.2.1: Erroneous Books ----


def screen_erroneous_book(bids: dict[float, float], asks: dict[float, float]) -> bool:
    """
    Returns True if the book should be discarded entirely.
    Rule 1: unparseable (handled upstream)
    Rule 2: no bids or no asks
    Rule 3: book crosses (best bid >= best ask)
    """
    if not bids or not asks:
        return True

    best_bid = max(bids.keys())
    best_ask = min(asks.keys())

    return best_bid >= best_ask


# ---- Section 5.2.2: Erroneous Prices ----


def filter_erroneous_prices(book_side: dict[float, float]) -> dict[float, float]:
    """
    Removes individual entries with non-numeric or non-positive price/size.
    Returns cleaned dict {price: size}.
    """
    cleaned: dict[float, float] = {}
    for price, size in book_side.items():
        if not isinstance(price, (int, float)) or not isinstance(size, (int, float)):
            continue
        if price <= 0 or size <= 0:
            continue
        cleaned[price] = size
    return cleaned


# ---- Section 5.3: Potentially Erroneous Data ----


def screen_potentially_erroneous(
    exchange_mids: dict[str, float],
    threshold: float = POTENTIALLY_ERRONEOUS_PARAM,
) -> set[str]:
    """
    Flag exchanges whose mid deviates > POTENTIALLY_ERRONEOUS_PARAM from median.
    Implements hysteresis (Section 5.3 step 4): once flagged, stays flagged until
    deviation drops below 50% of the parameter.
    Returns set of exchange names to discard.
    """
    if not exchange_mids:
        return set()

    median_mid = statistics.median(exchange_mids.values())
    if median_mid == 0:
        return _flagged_exchanges & set(exchange_mids.keys())

    currently_flagged: set[str] = set()
    for exchange, mid in exchange_mids.items():
        deviation = abs(mid - median_mid) / median_mid

        if exchange in _flagged_exchanges:
            # Step 4: reinstate only if deviation < 50% of threshold
            if deviation < threshold * 0.5:
                _flagged_exchanges.discard(exchange)
            else:
                currently_flagged.add(exchange)
        else:
            # Step 3: flag if deviation exceeds threshold
            if deviation > threshold:
                _flagged_exchanges.add(exchange)
                currently_flagged.add(exchange)

    return currently_flagged


def get_exchange_mid(
    bids: dict[float, float], asks: dict[float, float]
) -> float | None:
    """Mid price = (best bid + best ask) / 2."""
    if not bids or not asks:
        return None
    best_bid = max(bids.keys())
    best_ask = min(asks.keys())
    return (best_bid + best_ask) / 2


# ---- Steps 1-2: Consolidation ----


def _aggregate_book_levels(
    exchange_books: ExchangeBooks,
) -> tuple[dict[float, float], dict[float, float]]:
    all_bids: dict[float, float] = {}
    all_asks: dict[float, float] = {}

    for book in exchange_books.values():
        for price, size in book["bids"].items():
            if size <= 0:
                continue
            all_bids[price] = all_bids.get(price, 0.0) + size

        for price, size in book["asks"].items():
            if size <= 0:
                continue
            all_asks[price] = all_asks.get(price, 0.0) + size

    return all_bids, all_asks


def consolidate_books(
    exchange_books: ExchangeBooks, order_cap: float | None
) -> tuple[Levels, Levels]:
    """
    Merge all exchange orderbooks into one consolidated orderbook.
    Each price level's size is capped at order_cap (C_T).
    Returns (bids, asks) as sorted lists of (price, size).
    """
    all_bids, all_asks = _aggregate_book_levels(exchange_books)

    if order_cap is not None:
        all_bids = {price: min(size, order_cap) for price, size in all_bids.items()}
        all_asks = {price: min(size, order_cap) for price, size in all_asks.items()}

    bids = sorted(all_bids.items(), key=lambda x: x[0], reverse=True)
    asks = sorted(all_asks.items(), key=lambda x: x[0])
    return bids, asks


def consolidate_books_uncapped(exchange_books: ExchangeBooks) -> tuple[Levels, Levels]:
    """Merge without capping — used for dynamic order cap calculation."""
    return consolidate_books(exchange_books, None)


def uncross_consolidated_book(bids: Levels, asks: Levels) -> tuple[Levels, Levels]:
    """Remove executable cross-venue volume before building price-volume curves."""
    clean_bids = [[price, size] for price, size in bids]
    clean_asks = [[price, size] for price, size in asks]
    bid_idx = ask_idx = 0

    while (
        bid_idx < len(clean_bids)
        and ask_idx < len(clean_asks)
        and clean_bids[bid_idx][0] >= clean_asks[ask_idx][0]
    ):
        matched = min(clean_bids[bid_idx][1], clean_asks[ask_idx][1])
        clean_bids[bid_idx][1] -= matched
        clean_asks[ask_idx][1] -= matched
        if clean_bids[bid_idx][1] <= 0:
            bid_idx += 1
        if clean_asks[ask_idx][1] <= 0:
            ask_idx += 1

    return (
        [(price, size) for price, size in clean_bids[bid_idx:] if size > 0],
        [(price, size) for price, size in clean_asks[ask_idx:] if size > 0],
    )


# ---- Step 3: Price-Volume Curves (Eq. 1a-1f) ----


def compute_dynamic_spacing(
    bids: Levels, asks: Levels, target_points: int = 100
) -> float | None:
    """Choose a scale-free spacing that samples the shared depth about 100 times."""
    shared_depth = min(sum(size for _, size in bids), sum(size for _, size in asks))
    if shared_depth <= 0:
        return None
    # ponytail: depth spacing omits CF's KDE mode; add it if proxy tracking shows material error.
    return shared_depth / max(1, int(target_points))


def _prices_at_volumes(levels: Levels, volumes: list[float]) -> dict[float, float]:
    curve: dict[float, float] = {}
    cumulative = 0.0
    level_idx = 0
    for volume in volumes:
        while level_idx < len(levels) and cumulative + levels[level_idx][1] < volume:
            cumulative += levels[level_idx][1]
            level_idx += 1
        if level_idx >= len(levels):
            break
        curve[volume] = levels[level_idx][0]
    return curve


def compute_price_volume_curves(
    bids: Levels,
    asks: Levels,
    spacing: float = SPACING,
) -> tuple[
    dict[float, float], dict[float, float], dict[float, float], dict[float, float]
]:
    """
    Build askPV, bidPV, midPV, and midSV directly at spacing granularity.
    Runtime and memory are bounded by sampled points rather than base-asset units.
    """
    spacing = float(spacing)
    if not bids or not asks or spacing <= 0:
        return {}, {}, {}, {}

    shared_depth = min(sum(size for _, size in bids), sum(size for _, size in asks))
    point_count = min(50_000, int(shared_depth / spacing + 1e-12))
    volumes = [spacing * step for step in range(1, point_count + 1)]
    ask_pv = _prices_at_volumes(asks, volumes)
    bid_pv = _prices_at_volumes(bids, volumes)
    mid_pv: dict[float, float] = {}
    mid_sv: dict[float, float] = {}

    for volume in volumes:
        if volume not in ask_pv or volume not in bid_pv:
            break
        mid = (ask_pv[volume] + bid_pv[volume]) / 2
        mid_pv[volume] = mid
        mid_sv[volume] = (ask_pv[volume] / mid) - 1 if mid > 0 else float("inf")

    return ask_pv, bid_pv, mid_pv, mid_sv


# ---- Step 4: Utilized Depth (Eq. 2) ----


def compute_utilized_depth(
    mid_sv: dict[float, float],
    spacing: float = SPACING,
    deviation_threshold: float = DEVIATION_THRESHOLD,
) -> float:
    """
    v̄_T = max(v_i where midSV(v_i) <= D and midSV(v_{i+1}) > D, s)
    """
    if not mid_sv:
        return spacing

    volumes = sorted(mid_sv.keys())
    utilized = 0.0

    for i, v in enumerate(volumes):
        if mid_sv[v] <= deviation_threshold:
            utilized = v
        else:
            break

    return max(utilized, spacing)


# ---- Steps 5-6: Exponential Weighting (Eq. 3) ----


def compute_brti(
    mid_pv: dict[float, float],
    utilized_depth: float,
    spacing: float = SPACING,
    price_decimals: int = 2,
) -> float | None:
    """
    CCRTI_T = Σ_{v ∈ {s, 2s, ..., v̄_T}} midPV(v) * (1/NF) * λ * e^(-λv)
    λ = 1 / (0.3 * v̄_T)
    """
    if not mid_pv or utilized_depth < spacing:
        return None

    lam = 1.0 / (0.3 * utilized_depth)

    # Compute raw weights at spacing intervals
    raw_weights = {
        volume: lam * math.exp(-lam * volume)
        for volume in sorted(mid_pv)
        if volume <= utilized_depth + 1e-12
    }

    if not raw_weights:
        return None

    # NF: normalization factor so weights sum to 1
    nf = sum(raw_weights.values())
    if nf == 0:
        return None

    # BRTI = weighted sum
    brti = 0.0
    for v, weight in raw_weights.items():
        brti += mid_pv[v] * (weight / nf)

    return round(brti, max(0, min(12, int(price_decimals))))


# ---- Full Pipeline ----


def _filter_stale_books(
    exchange_books: ExchangeBooks,
    current_time: float,
    stale_threshold: float = STALE_THRESHOLD,
) -> ExchangeBooks:
    return {
        name: book
        for name, book in exchange_books.items()
        if 0 <= (current_time - book.get("last_update", 0)) < stale_threshold
    }


def _sanitize_exchange_books(exchange_books: ExchangeBooks) -> ExchangeBooks:
    sanitized: ExchangeBooks = {}
    for name, book in exchange_books.items():
        sanitized[name] = {
            "bids": filter_erroneous_prices(book["bids"]),
            "asks": filter_erroneous_prices(book["asks"]),
            "last_update": book["last_update"],
        }
    return sanitized


def _drop_erroneous_books(exchange_books: ExchangeBooks) -> ExchangeBooks:
    clean_books: ExchangeBooks = {}
    for name, book in exchange_books.items():
        if not screen_erroneous_book(book["bids"], book["asks"]):
            clean_books[name] = book
    return clean_books


def _drop_potentially_erroneous_books(
    exchange_books: ExchangeBooks,
    potentially_erroneous_param: float = POTENTIALLY_ERRONEOUS_PARAM,
) -> ExchangeBooks:
    exchange_mids: dict[str, float] = {}
    for name, book in exchange_books.items():
        mid = get_exchange_mid(book["bids"], book["asks"])
        if mid is not None:
            exchange_mids[name] = mid

    flagged = screen_potentially_erroneous(
        exchange_mids,
        threshold=potentially_erroneous_param,
    )
    return {name: book for name, book in exchange_books.items() if name not in flagged}


def calculate_brti(
    exchange_books: ExchangeBooks,
    current_time: float | None = None,
    *,
    spacing: float | None = None,
    deviation_threshold: float = DEVIATION_THRESHOLD,
    potentially_erroneous_param: float = POTENTIALLY_ERRONEOUS_PARAM,
    stale_threshold: float = STALE_THRESHOLD,
    price_decimals: int = 2,
) -> tuple[float | None, float, int]:
    """
    Synthetic CF-style RTI proxy calculation.
    Returns (brti_value, utilized_depth, num_exchanges_used) or (None, 0, 0) on failure.
    """
    if current_time is None:
        current_time = time.time()

    deviation_threshold = float(deviation_threshold)
    potentially_erroneous_param = float(potentially_erroneous_param)
    stale_threshold = float(stale_threshold)

    # --- Section 5.1: Stale data ---
    valid_books = _filter_stale_books(
        exchange_books, current_time, stale_threshold=stale_threshold
    )

    if not valid_books:
        return None, 0, 0

    # --- Section 5.2.2: Filter erroneous prices per exchange ---
    valid_books = _sanitize_exchange_books(valid_books)

    # --- Section 5.2.1: Flag erroneous books ---
    clean_books = _drop_erroneous_books(valid_books)

    if not clean_books:
        return None, 0, 0

    # --- Section 5.3: Potentially erroneous data (with hysteresis) ---
    final_books = _drop_potentially_erroneous_books(
        clean_books,
        potentially_erroneous_param=potentially_erroneous_param,
    )

    if not final_books:
        return None, 0, 0

    # --- Section 4.1.3: Dynamic order size cap ---
    uncapped_bids, uncapped_asks = consolidate_books_uncapped(final_books)
    order_cap = compute_dynamic_order_cap(uncapped_bids, uncapped_asks)

    # --- Steps 1-2: Consolidate with dynamic cap ---
    bids, asks = consolidate_books(final_books, order_cap)
    bids, asks = uncross_consolidated_book(bids, asks)

    if not bids or not asks:
        return None, 0, 0

    if spacing is None or float(spacing) <= 0:
        spacing = compute_dynamic_spacing(bids, asks)
    if spacing is None:
        return None, 0, 0
    spacing = float(spacing)

    # --- Step 3: Price-volume curves ---
    _, _, mid_pv, mid_sv = compute_price_volume_curves(
        bids, asks, spacing=spacing
    )

    if not mid_pv:
        return None, 0, 0

    # --- Step 4: Utilized depth ---
    utilized_depth = compute_utilized_depth(
        mid_sv,
        spacing=spacing,
        deviation_threshold=deviation_threshold,
    )

    # --- Steps 5-6: Exponential weighting ---
    brti = compute_brti(
        mid_pv,
        utilized_depth,
        spacing=spacing,
        price_decimals=price_decimals,
    )

    return brti, utilized_depth, len(final_books)
