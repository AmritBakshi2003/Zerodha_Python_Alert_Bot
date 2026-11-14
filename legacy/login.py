"""Daily login helper: prints the Kite login URL, exchanges the request_token
you paste back for an access_token, and saves it into .env."""
from dotenv import set_key
from kiteconnect import KiteConnect
from config import load_settings

s = load_settings()
kite = KiteConnect(api_key=s["api_key"])
print("1) Open this URL and log in:\n  ", kite.login_url())
req = input("2) Paste the request_token from the redirected URL: ").strip()
data = kite.generate_session(req, api_secret=s["api_secret"])
set_key(".env", "KITE_ACCESS_TOKEN", data["access_token"])
print("Saved KITE_ACCESS_TOKEN to .env — valid until ~6 AM tomorrow.")
