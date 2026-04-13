import math
import statistics
import time
from typing import TypedDict

# --- Index Methodology Parameters (from CME CF RTI methodology family) ---
# Section 6.2 style depth-walk parameters; applied to the active profile's spot orderbooks.
SPACING = 1                          # Volume spacing in base-asset units (e.g., 1 BTC / 1 ETH)
DEVIATION_THRESHOLD = 0.005          # D = 0.5%
POTENTIALLY_ERRONEOUS_PARAM = 0.05   # 5%
STALE_THRESHOLD = 30                 # Discard exchange if data >30s old

# Tracks exchanges flagged as potentially erroneous (Section 5.3 step 4 hysteresis)
_flagged_exchanges: set[str] = set()


class ExchangeBook(TypedDict):
    bids: dict[float, float]
    asks: dict[float, float]
    last_update: float


ExchangeBooks = dict[str, ExchangeBook]
Levels = list[tuple[float, float]]


def reset_brti_calc_state() -> None:
    """Clears cross-run hysteresis flags (used when switching between BTC and ETH)."""
    _flagged_exchanges.clear()


# ---- Section 4.1.3: Dynamic Order Size Cap (Eq. 4a-5) ----

def compute_dynamic_order_cap(uncapped_bids: Levels, uncapped_asks: Levels) -> float | None:
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
        trimmed_mean = sum(s_t[k:n_t - k]) / (n_t - 2 * k)

    # Eq 4f: winsorized sample set
    s_prime = []
    for i in range(n_t):
        if i < k:
            s_prime.append(s_t[k])        # replace low outliers with s_{k+1} (0-indexed: s_t[k])
        elif i >= n_t - k:
            s_prime.append(s_t[n_t - k - 1])  # replace high outliers with s_{n-k} (0-indexed: s_t[n_t-k-1])
        else:
            s_prime.append(s_t[i])

    # Eq 4g: winsorized mean
    winsorized_mean = sum(s_prime) / n_t

    # Eq 4h: winsorized sample standard deviation
    if n_t <= 1:
        sigma = 0
    else:
        variance = sum((x - winsorized_mean) ** 2 for x in s_prime) / (n_t - 1)
        sigma = math.sqrt(variance)

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

    # Crossed book
    if best_bid >= best_ask:
        return True

    return False


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
    global _flagged_exchanges

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


def get_exchange_mid(bids: dict[float, float], asks: dict[float, float]) -> float | None:
    """Mid price = (best bid + best ask) / 2."""
    if not bids or not asks:
        return None
    best_bid = max(bids.keys())
    best_ask = min(asks.keys())
    return (best_bid + best_ask) / 2


# ---- Steps 1-2: Consolidation ----

def _aggregate_book_levels(exchange_books: ExchangeBooks) -> tuple[dict[float, float], dict[float, float]]:
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


def consolidate_books(exchange_books: ExchangeBooks, order_cap: float | None) -> tuple[Levels, Levels]:
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
    all_bids, all_asks = _aggregate_book_levels(exchange_books)

    bids = sorted(all_bids.items(), key=lambda x: x[0], reverse=True)
    asks = sorted(all_asks.items(), key=lambda x: x[0])
    return bids, asks


# ---- Step 3: Price-Volume Curves (Eq. 1a-1f) ----

def _walk_raw_curve(levels: Levels) -> dict[int, float]:
    """
    Walk sorted price levels, compute marginal price at each integer volume.
    Eq 1a/1b: raw curve before spacing is applied.
    """
    curve: dict[int, float] = {}
    cumulative = 0.0
    level_idx = 0
    v = 1

    while level_idx < len(levels):
        price, size = levels[level_idx]
        next_cumulative = cumulative + size

        while v <= int(next_cumulative):
            curve[v] = price
            v += 1

        cumulative = next_cumulative
        level_idx += 1

    return curve


def compute_price_volume_curves(
    bids: Levels,
    asks: Levels,
    spacing: int = SPACING,
) -> tuple[dict[int, float], dict[int, float], dict[int, float], dict[int, float]]:
    """
    Step 3: Build askPV, bidPV, midPV, midSV at spacing granularity.
    Eq 1c: askPV(v) = raw_askPV(s * ceil(v/s))
    Eq 1d: bidPV(v) = raw_bidPV(s * ceil(v/s))
    For spacing=1, ceil(v/1) = v, so askPV = raw_askPV.
    """
    raw_ask = _walk_raw_curve(asks)
    raw_bid = _walk_raw_curve(bids)

    if not raw_ask or not raw_bid:
        return {}, {}, {}, {}

    max_raw = min(max(raw_ask.keys()), max(raw_bid.keys()))

    ask_pv: dict[int, float] = {}
    bid_pv: dict[int, float] = {}
    mid_pv: dict[int, float] = {}
    mid_sv: dict[int, float] = {}

    v = spacing
    while v <= max_raw:
        # Eq 1c/1d: evaluate raw curve at s * ceil(v/s)
        lookup = spacing * math.ceil(v / spacing)
        if lookup not in raw_ask or lookup not in raw_bid:
            break

        ask_pv[v] = raw_ask[lookup]
        bid_pv[v] = raw_bid[lookup]

        # Eq 1e: midPV
        mid = (ask_pv[v] + bid_pv[v]) / 2
        mid_pv[v] = mid

        # Eq 1f: midSV
        if mid > 0:
            mid_sv[v] = (ask_pv[v] / mid) - 1
        else:
            mid_sv[v] = float('inf')

        v += spacing

    return ask_pv, bid_pv, mid_pv, mid_sv


# ---- Step 4: Utilized Depth (Eq. 2) ----

def compute_utilized_depth(
    mid_sv: dict[int, float],
    spacing: int = SPACING,
    deviation_threshold: float = DEVIATION_THRESHOLD,
) -> int:
    """
    v̄_T = max(v_i where midSV(v_i) <= D and midSV(v_{i+1}) > D, s)
    """
    if not mid_sv:
        return spacing

    volumes = sorted(mid_sv.keys())
    utilized = 0

    for i, v in enumerate(volumes):
        if mid_sv[v] <= deviation_threshold:
            utilized = v
        else:
            break

    return max(utilized, spacing)


# ---- Steps 5-6: Exponential Weighting (Eq. 3) ----

def compute_brti(mid_pv: dict[int, float], utilized_depth: int, spacing: int = SPACING) -> float | None:
    """
    CCRTI_T = Σ_{v ∈ {s, 2s, ..., v̄_T}} midPV(v) * (1/NF) * λ * e^(-λv)
    λ = 1 / (0.3 * v̄_T)
    """
    if not mid_pv or utilized_depth < spacing:
        return None

    lam = 1.0 / (0.3 * utilized_depth)

    # Compute raw weights at spacing intervals
    raw_weights: dict[int, float] = {}
    v = spacing
    while v <= utilized_depth:
        if v not in mid_pv:
            break
        raw_weights[v] = lam * math.exp(-lam * v)
        v += spacing

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

    return round(brti, 2)


# ---- Full Pipeline ----

def _filter_stale_books(
    exchange_books: ExchangeBooks,
    current_time: float,
    stale_threshold: float = STALE_THRESHOLD,
) -> ExchangeBooks:
    return {
        name: book
        for name, book in exchange_books.items()
        if (current_time - book.get("last_update", 0)) < stale_threshold
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
    spacing: int = SPACING,
    deviation_threshold: float = DEVIATION_THRESHOLD,
    potentially_erroneous_param: float = POTENTIALLY_ERRONEOUS_PARAM,
    stale_threshold: float = STALE_THRESHOLD,
) -> tuple[float | None, int, int]:
    """
    Full BRTI calculation per CME CF Methodology v16.5.
    Returns (brti_value, utilized_depth, num_exchanges_used) or (None, 0, 0) on failure.
    """
    if current_time is None:
        current_time = time.time()

    spacing = max(1, int(spacing))

    try:
        deviation_threshold = float(deviation_threshold)
    except (TypeError, ValueError):
        deviation_threshold = DEVIATION_THRESHOLD
    if deviation_threshold <= 0:
        deviation_threshold = DEVIATION_THRESHOLD

    try:
        potentially_erroneous_param = float(potentially_erroneous_param)
    except (TypeError, ValueError):
        potentially_erroneous_param = POTENTIALLY_ERRONEOUS_PARAM
    if potentially_erroneous_param <= 0:
        potentially_erroneous_param = POTENTIALLY_ERRONEOUS_PARAM

    try:
        stale_threshold = float(stale_threshold)
    except (TypeError, ValueError):
        stale_threshold = STALE_THRESHOLD
    if stale_threshold <= 0:
        stale_threshold = STALE_THRESHOLD

    # --- Section 5.1: Stale data ---
    valid_books = _filter_stale_books(exchange_books, current_time, stale_threshold=stale_threshold)

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

    if not bids or not asks:
        return None, 0, 0

    # --- Step 3: Price-volume curves ---
    ask_pv, bid_pv, mid_pv, mid_sv = compute_price_volume_curves(bids, asks, spacing=spacing)

    if not mid_pv:
        return None, 0, 0

    # --- Step 4: Utilized depth ---
    utilized_depth = compute_utilized_depth(
        mid_sv,
        spacing=spacing,
        deviation_threshold=deviation_threshold,
    )

    # --- Steps 5-6: Exponential weighting ---
    brti = compute_brti(mid_pv, utilized_depth, spacing=spacing)

    return brti, utilized_depth, len(final_books)
