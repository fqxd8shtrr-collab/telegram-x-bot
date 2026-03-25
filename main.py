import os
import time
import sqlite3
import threading
import requests

BEARER_TOKEN = os.getenv("BEARER_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "15"))

DB_FILE = "data.db"


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT UNIQUE NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sent_posts (
            post_id TEXT PRIMARY KEY
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.commit()
    conn.close()


def db_execute(query, params=(), fetch=False, fetchone=False):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(query, params)

    result = None
    if fetchone:
        result = cur.fetchone()
    elif fetch:
        result = cur.fetchall()

    conn.commit()
    conn.close()
    return result


def add_keyword(keyword):
    keyword = keyword.strip()
    if not keyword:
        return False, "الكلمة فارغة"

    try:
        db_execute("INSERT INTO keywords (keyword) VALUES (?)", (keyword,))
        return True, f"تمت إضافة: {keyword}"
    except sqlite3.IntegrityError:
        return False, "هذه الكلمة موجودة مسبقًا"


def remove_keyword(keyword):
    keyword = keyword.strip()
    row = db_execute("SELECT id FROM keywords WHERE keyword = ?", (keyword,), fetchone=True)
    if not row:
        return False, "هذه الكلمة غير موجودة"

    db_execute("DELETE FROM keywords WHERE keyword = ?", (keyword,))
    return True, f"تم حذف: {keyword}"


def list_keywords():
    rows = db_execute("SELECT keyword FROM keywords ORDER BY id DESC", fetch=True)
    return [r[0] for r in rows]


def already_sent(post_id):
    row = db_execute("SELECT 1 FROM sent_posts WHERE post_id = ?", (post_id,), fetchone=True)
    return row is not None


def mark_sent(post_id):
    db_execute("INSERT OR IGNORE INTO sent_posts (post_id) VALUES (?)", (post_id,))


def get_offset():
    row = db_execute("SELECT value FROM bot_state WHERE key = 'update_offset'", fetchone=True)
    return int(row[0]) if row and row[0] else 0


def set_offset(offset):
    db_execute("""
        INSERT INTO bot_state (key, value)
        VALUES ('update_offset', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (str(offset),))


def search_x(keyword):
    url = "https://api.x.com/2/tweets/search/recent"
    headers = {
        "Authorization": "Bearer " + BEARER_TOKEN.strip()
    }
    params = {
        "query": f'({keyword}) has:videos -is:retweet',
        "max_results": 10,
        "tweet.fields": "created_at,attachments,text",
        "expansions": "attachments.media_keys",
        "media.fields": "type,preview_image_url,variants"
    }

    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def extract_best_video_url(response_json, tweet):
    includes = response_json.get("includes", {})
    media_list = includes.get("media", [])
    media_map = {m["media_key"]: m for m in media_list if "media_key" in m}

    attachments = tweet.get("attachments", {})
    media_keys = attachments.get("media_keys", [])

    best_video_url = None
    best_bitrate = -1

    for key in media_keys:
        media = media_map.get(key)
        if not media:
            continue

        if media.get("type") != "video":
            continue

        variants = media.get("variants", [])
        for variant in variants:
            url = variant.get("url")
            content_type = variant.get("content_type", "")
            bitrate = variant.get("bit_rate", 0)

            if not url:
                continue
            if "video/mp4" not in content_type:
                continue

            if bitrate > best_bitrate:
                best_bitrate = bitrate
                best_video_url = url

    return best_video_url


def telegram_api(method, data=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/" + method
    r = requests.post(url, data=data or {}, timeout=60)
    r.raise_for_status()
    return r.json()


def telegram_get_updates():
    offset = get_offset()
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {
        "timeout": 30,
        "offset": offset
    }
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def send_message(text):
    return telegram_api("sendMessage", {
        "chat_id": CHAT_ID,
        "text": text[:4096]
    })


def reply_message(chat_id, text):
    return telegram_api("sendMessage", {
        "chat_id": str(chat_id),
        "text": text[:4096]
    })


def send_video(video_url, caption):
    return telegram_api("sendVideo", {
        "chat_id": CHAT_ID,
        "video": video_url,
        "caption": caption[:1024]
    })


def handle_command(text, chat_id):
    text = text.strip()

    if text.startswith("/add "):
        keyword = text[5:].strip()
        ok, msg = add_keyword(keyword)
        reply_message(chat_id, msg)
        return

    if text.startswith("/remove "):
        keyword = text[8:].strip()
        ok, msg = remove_keyword(keyword)
        reply_message(chat_id, msg)
        return

    if text == "/list":
        keywords = list_keywords()
        if not keywords:
            reply_message(chat_id, "لا توجد كلمات محفوظة")
        else:
            reply_message(chat_id, "الكلمات الحالية:\n\n" + "\n".join(f"- {k}" for k in keywords))
        return

    if text == "/start":
        reply_message(
            chat_id,
            "الأوامر:\n"
            "/add كلمة\n"
            "/remove كلمة\n"
            "/list"
        )
        return


def poll_telegram_commands():
    while True:
        try:
            updates = telegram_get_updates()
            for item in updates.get("result", []):
                update_id = item["update_id"]
                set_offset(update_id + 1)

                message = item.get("message") or item.get("channel_post")
                if not message:
                    continue

                chat_id = message["chat"]["id"]
                text = message.get("text", "")

                if text.startswith("/"):
                    handle_command(text, chat_id)

        except Exception as e:
            print("TELEGRAM ERROR:", e)

        time.sleep(2)


def process_keywords():
    keywords = list_keywords()
    if not keywords:
        return

    for keyword in keywords:
        try:
            data = search_x(keyword)
            tweets = data.get("data", [])
            tweets = list(reversed(tweets))

            for tweet in tweets:
                post_id = tweet["id"]

                if already_sent(post_id):
                    continue

                video_url = extract_best_video_url(data, tweet)
                post_url = f"https://x.com/i/web/status/{post_id}"
                caption = f"{tweet.get('text', '')}\n\n{post_url}\n\nKeyword: {keyword}"

                if video_url:
                    result = send_video(video_url, caption)
                    if result.get("ok"):
                        mark_sent(post_id)
                    else:
                        fallback = f"{tweet.get('text', '')}\n\n{post_url}\n\nKeyword: {keyword}\n\nVideo URL:\n{video_url}"
                        msg_result = send_message(fallback)
                        if msg_result.get("ok"):
                            mark_sent(post_id)
                else:
                    msg_result = send_message(caption)
                    if msg_result.get("ok"):
                        mark_sent(post_id)

        except Exception as e:
            print(f"X ERROR [{keyword}]:", e)


def main():
    if not BEARER_TOKEN or not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing required environment variables.")

    init_db()

    t = threading.Thread(target=poll_telegram_commands, daemon=True)
    t.start()

    while True:
        process_keywords()
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
