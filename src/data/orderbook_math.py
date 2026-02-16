def parse_orderbook(raw_ob: dict) -> tuple:
    """
    Converts raw orderbook into sorted bids and implied ask for both YES and NO.
    """

    raw_yes = raw_ob.get('yes') or []
    raw_no = raw_ob.get('no') or []
    
    # Sort bids highest-to-lowest
    yes_bids = sorted(raw_yes, key=lambda x: x[0], reverse=True)
    no_bids = sorted(raw_no, key=lambda x: x[0], reverse=True)
    
    # Calculate implied asks based on binary reciprocals
    yes_asks = [[100 - p, q] for p, q in no_bids]
    no_asks = [[100 - p, q] for p, q in yes_bids]
    
    return yes_bids, yes_asks, no_bids, no_asks