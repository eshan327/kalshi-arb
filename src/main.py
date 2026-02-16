import sys
import asyncio
from core.config import EXECUTION_MODE, KALSHI_ENV
from core.auth import get_authenticated_client
from engine.streamer import run_market_streamer

def main():
    print(f"\nStarting Kalshi Streaming")
    print(f"Environment: {KALSHI_ENV.upper()} | Execution Mode: {EXECUTION_MODE}")
    
    try:
        client = get_authenticated_client()
        balance_res = client.get_balance()
        print(f"Balance: ${balance_res.balance / 100:,.2f}\n")
    except Exception as e:
        print(f"Authentication Failed: {e}")
        sys.exit(1)

    # Starting async event loop
    try:
        asyncio.run(run_market_streamer())
    except KeyboardInterrupt:
        print("\nStreaming ended.")

if __name__ == "__main__":
    main()