import requests
import time
import os

BEARER_TOKEN = os.getenv("BEARER_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

KEYWORD = "drone"
LAST_ID = None

def search():
    global LAST_ID
    url = "https://api.twitter.com/2/tweets/search/recent"
    
    headers = {
        "Authorization": f"Bearer {BEARER_TOKEN}"
    }

    params = {
        "query": f"{KEYWORD} has:videos -is:retweet",
        "max_results": 10,
        "tweet.fields": "created_at"
    }

    res = requests.get(url, headers=headers, params=params)
    data = res.json()

    if "data" not in data:
        return

    for tweet in data["data"]:
        if tweet["id"] == LAST_ID:
            continue

        LAST_ID = tweet["id"]

        send_to_telegram(tweet["text"], tweet["id"])


def send_to_telegram(text, tweet_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    message = f"{text}\nhttps://twitter.com/i/web/status/{tweet_id}"

    requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": message
    })


while True:
    search()
    time.sleep(10)
