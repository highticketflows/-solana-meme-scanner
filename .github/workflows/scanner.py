import os
import requests
import time
from datetime import datetime

BIRDEYE_API_KEY = os.environ.get("BIRDEYE_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")  # We'll fill this later

# Track seen tokens to avoid duplicates
seen_tokens = set()

def get_new_tokens():
    """Fetch latest Solana tokens from Birdeye"""
    url = "https://public-api.birdeye.so/defi/v2/tokens/new_listing"
    headers = {"X-API-KEY": BIRDEYE_API_KEY}
    params = {
        "limit": 20,
        "chain": "solana"
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", {}).get("items", [])
    except Exception as e:
        print(f"API Error: {e}")
        return []

def send_to_telegram(message):
    """Send alert to Telegram"""
    if not CHAT_ID:
        print("No CHAT_ID set — skipping Telegram message")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=15)
        return True
    except Exception as e:
        print(f"Telegram Error: {e}")
        return False

def scan():
    print(f"🔍 Scanning at {datetime.now()}")
    tokens = get_new_tokens()
    
    for token in tokens:
        addr = token.get("address")
        name = token.get("name", "Unknown")
        sym = token.get("symbol", "???")
        
        if not addr or addr in seen_tokens:
            continue
        
        seen_tokens.add(addr)
        dex_url = f"https://dexscreener.com/solana/{addr}"
        birdeye_url = f"https://birdeye.so/token/{addr}?chain=solana"
        
        msg = f"""
🚀 *NEW TOKEN DETECTED*
📌 {name} ({sym})
🔗 Address: `{addr[:8]}...`
📊 [Birdeye]({birdeye_url}) | [DexScreener]({dex_url})
        """
        print(f"✅ New token: {name}")
        send_to_telegram(msg)
    
    print(f"✅ Scan complete — {len(seen_tokens)} total seen")

if __name__ == "__main__":
    scan()