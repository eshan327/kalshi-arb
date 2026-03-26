import asyncio
import logging
import uuid
from core.config import EXECUTION_MODE
from kalshi_python_sync import KalshiClient
from kalshi_python_sync.models.create_order_request import CreateOrderRequest

logger = logging.getLogger(__name__)

def execute_trade_decision(client: KalshiClient, ticker: str, side: str, price_cents: int, count: int = 1):
    """Routes a trade signal based on the active EXECUTION_MODE."""

    logger.info("[STRATEGY] Signal: buy %s '%s' contract(s) at %s cents", count, side.upper(), price_cents)
    
    if EXECUTION_MODE == "OBSERVE":
        logger.info("[OBSERVE MODE] Signal ignored. Taking no action.")
        return
        
    if EXECUTION_MODE == "PAPER":
        logger.info("[PAPER TRADING] Simulating order execution locally.")
        # TODO: Future paper trading tracking logic goes here
        return
        
    if EXECUTION_MODE == "LIVE":
        logger.info("[LIVE EXECUTION] Routing order to Kalshi now.")
        order = place_buy_limit_order(client, ticker, side, price_cents, count)
        if order:
            logger.info("[LIVE] Order placed. ID=%s", order.order_id)
            # TODO: future live order tracking logic goes here
            cancel_order(client, order.order_id)
            logger.info("[LIVE] Safety test: Order canceled.")


async def execute_trade_decision_async(
    client: KalshiClient,
    ticker: str,
    side: str,
    price_cents: int,
    count: int = 1,
):
    """Runs sync execution logic in a thread to avoid blocking an asyncio event loop."""
    await asyncio.to_thread(execute_trade_decision, client, ticker, side, price_cents, count)

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
        logger.exception("Order execution failed: %s", e)
        return None

def get_order_status(client: KalshiClient, order_id: str):
    """Fetches the current status of a resting order."""

    try:
        response = client.get_order(order_id)
        return response.order
    except Exception as e:
        logger.exception("Couldn't fetch order status: %s", e)
        return None

def cancel_order(client: KalshiClient, order_id: str):
    """Cancels a resting order by its ID."""

    try:
        response = client.cancel_order(order_id)
        return response.order
    except Exception as e:
        logger.exception("Order cancellation failed: %s", e)
        return None