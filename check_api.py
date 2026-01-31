#!/usr/bin/env python3
"""Check that the Hope VPN API is reachable from this machine (same config as the bot)."""
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import httpx
except ImportError:
    print("Install httpx: pip install httpx")
    sys.exit(1)

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:3000").rstrip("/")
HEALTH_URL = f"{API_BASE_URL}/health"

def main():
    print(f"Checking API at {HEALTH_URL} ...")
    try:
        r = httpx.get(HEALTH_URL, timeout=5.0)
        r.raise_for_status()
        data = r.json()
        if data.get("ok"):
            print("OK – API is reachable. Bot can connect to your local dev server.")
            return 0
        print("API responded but ok != true:", data)
        return 1
    except httpx.ConnectError as e:
        print(f"Connection failed – cannot reach {API_BASE_URL}")
        print("  Make sure the API is running: cd hope-vpn-api && node src/index.js")
        print(f"  Error: {e}")
        return 1
    except httpx.TimeoutException:
        print(f"Timeout – {HEALTH_URL} did not respond in 5s")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
