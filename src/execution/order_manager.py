import uuid
from core.config import EXECUTION_MODE
from kalshi_python_sync import KalshiClient
from kalshi_python_sync.models.create_order_request import CreateOrderRequest

def execute_trade_decision(client: KalshiClient, ticker: str, side: str, price_cents: int, count: int = 1):
    """Routes a trade signal based on the active EXECUTION_MODE."""

    print(f"  --> [STRATEGY] Signal: buy {count} '{side.upper()}' contract(s) at {price_cents}¢")
    
    if EXECUTION_MODE == "OBSERVE":
        print("  --> [OBSERVE MODE] Signal ignored. Taking no action.")
        return
        
    if EXECUTION_MODE == "PAPER":
        print("  --> [PAPER TRADING] Simulating order execution locally.")
        # TODO: Future paper trading tracking logic goes here
        return
        
    if EXECUTION_MODE == "LIVE":
        print("  --> [LIVE EXECUTION] Routing order to Kalshi now.")
        order = place_buy_limit_order(client, ticker, side, price_cents, count)
        if order:
            print(f"  --> [LIVE] Order Placed! ID: {order.order_id}")
            # TODO: future live order tracking logic goes here
            cancel_order(client, order.order_id)
            print("  --> [LIVE] Safety test: Order canceled.")

def place_buy_limit_order(client: KalshiClient, ticker: str, side: str, price_cents: int, count: int = 1):
    """Submits a limit order using a local UUID."""

    client_order_id = str(uuid.uuid4())
    
    order_params = {
        "ticker": ticker,
        "action": "buy",
        "side": side.lower(),
        "type": "limit",
        "count": count,
        "client_order_id": client_order_id
    }
    
    if side.lower() == 'yes':
        order_params['yes_price'] = price_cents
    elif side.lower() == 'no':
        order_params['no_price'] = price_cents

    req = CreateOrderRequest(**order_params)
    
    try:
        response = client.create_order(req)
        return response.order
    except Exception as e:
        print(f" Order execution failed: {e}")
        return None

def get_order_status(client: KalshiClient, order_id: str):
    """Fetches the current status of a resting order."""

    try:
        response = client.get_order(order_id)
        return response.order
    except Exception as e:
        print(f" Couldn't fetch order status: {e}")
        return None

def cancel_order(client: KalshiClient, order_id: str):
    """Cancels a resting order by its ID."""

    try:
        response = client.cancel_order(order_id)
        return response.order
    except Exception as e:
        print(f" Order cancellation failed: {e}")
        return None