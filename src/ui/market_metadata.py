def extract_suggested_strike(market_info: dict) -> float | None:
    """Best-effort strike extraction from Kalshi market metadata."""
    if not market_info:
        return None

    direct_keys = [
        "strike_price",
        "strike",
        "target_price",
        "floor_strike",
        "cap_strike",
    ]
    for key in direct_keys:
        value = market_info.get(key)
        if isinstance(value, (int, float)):
            return float(value)

    text_keys = ["subtitle", "title", "yes_sub_title", "no_sub_title", "rulebook_text"]
    for key in text_keys:
        text = market_info.get(key)
        if not isinstance(text, str):
            continue

        cleaned = text.replace(",", "")
        token = ""
        matches = []
        for ch in cleaned:
            if ch.isdigit() or ch == ".":
                token += ch
            else:
                if token:
                    matches.append(token)
                    token = ""
        if token:
            matches.append(token)

        for candidate in matches:
            try:
                value = float(candidate)
            except ValueError:
                continue
            if 1000 <= value <= 2_000_000:
                return value

    return None
