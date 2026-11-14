"""Alert delivery. Alerts only — this project never imports or calls any order API."""
import logging
import time
import requests

log = logging.getLogger("notifier")


def _post_with_retry(url, payload, retries=4):
    for i in range(retries):
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 429:  # rate limited -> honour Retry-After
                wait = float(r.headers.get("Retry-After", 2 ** i))
                time.sleep(wait)
                continue
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            log.warning("notify attempt %d failed: %s", i + 1, e)
            time.sleep(2 ** i)
    return False


class Notifier:
    def __init__(self, channels, telegram_token="", telegram_chat="", slack_webhook=""):
        self.channels = channels
        self.tg_token, self.tg_chat, self.slack = telegram_token, telegram_chat, slack_webhook

    def send(self, text: str):
        if "console" in self.channels:
            print(text, flush=True)
        if "telegram" in self.channels and self.tg_token and self.tg_chat:
            _post_with_retry(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                             {"chat_id": self.tg_chat, "text": text})
        if "slack" in self.channels and self.slack:
            _post_with_retry(self.slack, {"text": text})
