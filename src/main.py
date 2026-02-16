import sys
from core.config import EXECUTION_MODE, KALSHI_ENV
from core.auth import get_authenticated_client
from engine.scanner import run_market_scanner

def main():
    print(f"Environment: {KALSHI_ENV.upper()} | Execution Mode: {EXECUTION_MODE}")
    
    try:
        client = get_authenticated_client()
        balance_res = client.get_balance()
        print(f"Balance: ${balance_res.balance / 100:,.2f}")
    except Exception as e:
        print(f"Authentication Failed: {e}")
        sys.exit(1)

    # Start the scanning loop
    run_market_scanner(client)

if __name__ == "__main__":
    main()