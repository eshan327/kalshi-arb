from datetime import datetime, timezone

def print_market_timing(open_time_str: str, close_time_str: str):
    """
    Calculates and prints the market window, current time, and a live countdown.
    """
        
    # Timestamp parsing & conversion to EST
    open_dt = datetime.fromisoformat(open_time_str.replace('Z', '+00:00'))
    close_dt = datetime.fromisoformat(close_time_str.replace('Z', '+00:00'))
    now = datetime.now(timezone.utc)
    local_open = open_dt.astimezone().strftime("%I:%M:%S %p")
    local_close = close_dt.astimezone().strftime("%I:%M:%S %p")
    local_now = now.astimezone().strftime("%I:%M:%S %p")
    
    # Elapsed + remaining time calculations
    elapsed_sec = int((now - open_dt).total_seconds())
    remaining_sec = int((close_dt - now).total_seconds())
    elapsed_sec = max(0, elapsed_sec)
    remaining_sec = max(0, remaining_sec)
    e_mins, e_secs = divmod(elapsed_sec, 60)
    r_mins, r_secs = divmod(remaining_sec, 60)
    
    print(f"  Window  : {local_open} to {local_close}")
    print(f"  Current : {local_now}")
    print(f"  Timer   : {e_mins:02d}:{e_secs:02d} elapsed | {r_mins:02d}:{r_secs:02d} remaining\n")


def print_orderbook_table(title: str, bids: list, asks: list, depth: int = 5):
    """
    Prints orderbook tables up to depth for terminal viewing.
    """

    # Table alignment
    w_bqty, w_bpx, w_apx, w_aqty = 7, 5, 5, 7
    header = f"{'Bid Qty':>{w_bqty}} | {'Bid ¢':>{w_bpx}} || {'Ask ¢':>{w_apx}} | {'Ask Qty':>{w_aqty}}"
    table_width = len(header)
    formatted_title = f"--- {title} ---"
    print(f"\n  {formatted_title.center(table_width)}")
    print(f"  {header}")
    print(f"  {'-' * table_width}")
    
    # Slice orderbook to the depth parameter
    bids = bids[:depth] if bids else []
    asks = asks[:depth] if asks else []
    max_rows = max(len(bids), len(asks))
    
    # Empty book handling
    if max_rows == 0:
        print(f"  {' ' * 10}(Empty Book)")
        return
        
    for i in range(max_rows):
        b_qty = bids[i][1] if i < len(bids) else ""
        b_px  = bids[i][0] if i < len(bids) else ""
        a_px  = asks[i][0] if i < len(asks) else ""
        a_qty = asks[i][1] if i < len(asks) else ""
        
        # Format strings using the exact widths defined above
        print(f"  {b_qty:>{w_bqty}} | {b_px:>{w_bpx}} || {a_px:>{w_apx}} | {a_qty:>{w_aqty}}")