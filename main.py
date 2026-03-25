import os
import time
import json
import sqlite3
import threading
from datetime import datetime, timezone

import requests


# =========================
# ENV
# =========================
BEARER_TOKEN = os.getenv("BEARER_TOKEN", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()  # مكان إرسال النتائج
CHECK_INTERVAL_DEFAULT = int(os.getenv("CHECK_INTERVAL", "15"))
CONTROL_CHAT_ID = os.getenv("CONTROL_CHAT_ID", "").strip()  # اختياري: آيديك للتحكم فقط

DB_FILE = "data.db"


# =========================
# DB
# =========================
def db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT UNIQUE NOT NULL,
            activated_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sent_posts (
            post_id TEXT PRIMARY KEY,
            keyword TEXT,
            sent_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            chat_id TEXT PRIMARY KEY,
            pending_action TEXT,
            updated_at TEXT
        )
    """)

    # إعدادات افتراضية
    upsert_bot_state("check_interval", str(CHECK_INTERVAL_DEFAULT))
    upsert_bot_state("is_paused", "0")
    upsert_bot_state("update_offset", "0")

    conn.commit()
    conn.close()


def upsert_bot_state(key, value):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO bot_state (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, value))
    conn.commit()
    conn.close()


def get_bot_state(key, default=None):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default


def set_user_pending_action(chat_id, action):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_state (chat_id, pending_action, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            pending_action=excluded.pending_action,
            updated_at=excluded.updated_at
    """, (str(chat_id), action, now_iso()))
    conn.commit()
    conn.close()


def get_user_pending_action(chat_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT pending_action FROM user_state WHERE chat_id = ?", (str(chat_id),))
    row = cur.fetchone()
    conn.close()
    return row["pending_action"] if row else None


def clear_user_pending_action(chat_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM user_state WHERE chat_id = ?", (str(chat_id),))
    conn.commit()
    conn.close()


def add_keyword(keyword):
    keyword = keyword.strip()
    if not keyword:
        return False, "الكلمة فارغة."

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO keywords (keyword, activated_at)
            VALUES (?, ?)
        """, (keyword, now_iso()))
        conn.commit()
        return True, f"تمت إضافة الكلمة:\n{keyword}"
    except sqlite3.IntegrityError:
        return False, "هذه الكلمة موجودة مسبقًا."
    finally:
        conn.close()


def remove_keyword(keyword):
    keyword = keyword.strip()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM keywords WHERE keyword = ?", (keyword,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False, "هذه الكلمة غير موجودة."

    cur.execute("DELETE FROM keywords WHERE keyword = ?", (keyword,))
    conn.commit()
    conn.close()
    return True, f"تم حذف الكلمة:\n{keyword}"


def list_keywords():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT keyword, activated_at FROM keywords ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return rows


def mark_sent(post_id, keyword):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO sent_posts (post_id, keyword, sent_at)
        VALUES (?, ?, ?)
    """, (post_id, keyword, now_iso()))
    conn.commit()
    conn.close()


def already_sent(post_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sent_posts WHERE post_id = ?", (post_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def get_update_offset():
    return int(get_bot_state("update_offset", "0"))


def set_update_offset(offset):
    upsert_bot_state("update_offset", str(offset))


# =========================
# HELPERS
# =========================
def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(dt_str):
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def is_admin_chat(chat_id):
    if not CONTROL_CHAT_ID:
        return True
    return str(chat_id) == str(CONTROL_CHAT_ID)


def get_check_interval():
    return int(get_bot_state("check_interval", str(CHECK_INTERVAL_DEFAULT)))


def is_paused():
    return get_bot_state("is_paused", "0") == "1"


def set_paused(value: bool):
    upsert_bot_state("is_paused", "1" if value else "0")


# =========================
# TELEGRAM
# =========================
def tg_api(method, data=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    r = requests.post(url, data=data or {}, timeout=60)
    r.raise_for_status()
    return r.json()


def tg_get_updates():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {
        "timeout": 30,
        "offset": get_update_offset()
    }
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def main_menu_keyboard():
    keyboard = {
        "keyboard": [
            [{"text": "➕ إضافة كلمة"}, {"text": "➖ حذف كلمة"}],
            [{"text": "📋 عرض الكلمات"}, {"text": "⏱ تغيير وقت الفحص"}],
            [{"text": "▶️ تشغيل الرصد"}, {"text": "⏸ إيقاف الرصد"}],
            [{"text": "📊 الحالة"}, {"text": "🧪 اختبار الإرسال"}],
            [{"text": "❌ إلغاء"}]
        ],
        "resize_keyboard": True,
        "is_persistent": True
    }
    return json.dumps(keyboard, ensure_ascii=False)


def send_message(chat_id, text, with_menu=False):
    data = {
        "chat_id": str(chat_id),
        "text": text[:4096]
    }
    if with_menu:
        data["reply_markup"] = main_menu_keyboard()
    return tg_api("sendMessage", data)


def send_video(chat_id, video_url, caption):
    data = {
        "chat_id": str(chat_id),
        "video": video_url,
        "caption": caption[:1024]
    }
    return tg_api("sendVideo", data)


def send_target_message(text):
    return send_message(CHAT_ID, text, with_menu=False)


def send_target_video(video_url, caption):
    return send_video(CHAT_ID, video_url, caption)


# =========================
# X SEARCH
# =========================
def search_x(keyword):
    url = "https://api.x.com/2/tweets/search/recent"
    headers = {
        "Authorization": "Bearer " + BEARER_TOKEN
    }
    params = {
        "query": f'({keyword}) has:videos -is:retweet',
        "max_results": 10,
        "tweet.fields": "created_at,attachments,text,author_id",
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


# =========================
# BOT UI / ACTIONS
# =========================
def status_text():
    rows = list_keywords()
    interval = get_check_interval()
    paused = "متوقف" if is_paused() else "يعمل"
    return (
        f"📊 حالة البوت\n\n"
        f"الحالة: {paused}\n"
        f"وقت الفحص: {interval} ثانية\n"
        f"عدد الكلمات: {len(rows)}\n"
        f"وجهة الإرسال: {CHAT_ID}"
    )


def handle_menu_text(chat_id, text):
    text = (text or "").strip()

    if text == "/start":
        clear_user_pending_action(chat_id)
        send_message(
            chat_id,
            "مرحبًا بك.\nاختر من الأزرار بالأسفل:",
            with_menu=True
        )
        return

    if text == "➕ إضافة كلمة":
        set_user_pending_action(chat_id, "add_keyword")
        send_message(chat_id, "أرسل الآن الكلمة أو الجملة التي تريد إضافتها.", with_menu=True)
        return

    if text == "➖ حذف كلمة":
        set_user_pending_action(chat_id, "remove_keyword")
        send_message(chat_id, "أرسل الآن الكلمة التي تريد حذفها بالضبط.", with_menu=True)
        return

    if text == "📋 عرض الكلمات":
        rows = list_keywords()
        if not rows:
            send_message(chat_id, "لا توجد كلمات محفوظة.", with_menu=True)
            return

        msg = "📋 الكلمات الحالية:\n\n"
        for row in rows:
            msg += f"- {row['keyword']}\n"
        send_message(chat_id, msg, with_menu=True)
        return

    if text == "⏱ تغيير وقت الفحص":
        set_user_pending_action(chat_id, "set_interval")
        send_message(chat_id, "أرسل الوقت بالثواني.\nمثال: 5", with_menu=True)
        return

    if text == "▶️ تشغيل الرصد":
        set_paused(False)
        send_message(chat_id, "تم تشغيل الرصد.", with_menu=True)
        return

    if text == "⏸ إيقاف الرصد":
        set_paused(True)
        send_message(chat_id, "تم إيقاف الرصد.", with_menu=True)
        return

    if text == "📊 الحالة":
        send_message(chat_id, status_text(), with_menu=True)
        return

    if text == "🧪 اختبار الإرسال":
        send_target_message("✅ اختبار ناجح: البوت يرسل بشكل صحيح.")
        send_message(chat_id, "تم إرسال رسالة اختبار إلى الوجهة المحددة.", with_menu=True)
        return

    if text == "❌ إلغاء" or text == "/cancel":
        clear_user_pending_action(chat_id)
        send_message(chat_id, "تم إلغاء العملية.", with_menu=True)
        return

    # إذا كان المستخدم داخل خطوة انتظار
    pending = get_user_pending_action(chat_id)
    if pending:
        handle_pending_action(chat_id, pending, text)
        return

    # غير معروف
    send_message(chat_id, "اختر من الأزرار بالأسفل.", with_menu=True)


def handle_pending_action(chat_id, pending, text):
    text = text.strip()

    if pending == "add_keyword":
        ok, msg = add_keyword(text)
        clear_user_pending_action(chat_id)
        send_message(chat_id, msg, with_menu=True)
        return

    if pending == "remove_keyword":
        ok, msg = remove_keyword(text)
        clear_user_pending_action(chat_id)
        send_message(chat_id, msg, with_menu=True)
        return

    if pending == "set_interval":
        if not text.isdigit():
            send_message(chat_id, "أرسل رقمًا فقط.\nمثال: 5", with_menu=True)
            return

        seconds = int(text)
        if seconds < 3:
            send_message(chat_id, "الحد الأدنى الموصى به هو 3 ثوانٍ.", with_menu=True)
            return

        upsert_bot_state("check_interval", str(seconds))
        clear_user_pending_action(chat_id)
        send_message(chat_id, f"تم تغيير وقت الفحص إلى {seconds} ثانية.", with_menu=True)
        return


# =========================
# POLL TELEGRAM
# =========================
def poll_telegram():
    while True:
        try:
            updates = tg_get_updates()
            for item in updates.get("result", []):
                update_id = item["update_id"]
                set_update_offset(update_id + 1)

                message = item.get("message")
                if not message:
                    continue

                chat_id = message["chat"]["id"]
                text = message.get("text", "")

                if not is_admin_chat(chat_id):
                    send_message(chat_id, "غير مصرح لك بالتحكم بهذا البوت.")
                    continue

                handle_menu_text(chat_id, text)

        except Exception as e:
            print("TELEGRAM ERROR:", e)

        time.sleep(2)


# =========================
# MONITOR LOOP
# =========================
def process_keywords():
    if is_paused():
        return

    rows = list_keywords()
    if not rows:
        return

    for row in rows:
        keyword = row["keyword"]
        activated_at = parse_iso(row["activated_at"])

        try:
            data = search_x(keyword)
            tweets = data.get("data", [])
            tweets = list(reversed(tweets))  # الأقدم أولًا

            for tweet in tweets:
                post_id = tweet["id"]
                if already_sent(post_id):
                    continue

                created_at_str = tweet.get("created_at")
                if not created_at_str:
                    continue

                created_at = parse_iso(created_at_str)

                # لا يرسل إلا ما نُشر بعد وقت إضافة الكلمة
                if created_at < activated_at:
                    continue

                post_url = f"https://x.com/i/web/status/{post_id}"
                text = tweet.get("text", "")
                caption = f"{text}\n\n{post_url}\n\nKeyword: {keyword}"

                video_url = extract_best_video_url(data, tweet)

                if video_url:
                    try:
                        result = send_target_video(video_url, caption)
                        if result.get("ok"):
                            mark_sent(post_id, keyword)
                            continue
                    except Exception as send_err:
                        print("VIDEO SEND ERROR:", send_err)

                # fallback
                try:
                    msg_result = send_target_message(caption)
                    if msg_result.get("ok"):
                        mark_sent(post_id, keyword)
                except Exception as msg_err:
                    print("MESSAGE SEND ERROR:", msg_err)

        except Exception as e:
            print(f"X ERROR [{keyword}]:", e)


def monitor_loop():
    while True:
        try:
            process_keywords()
        except Exception as e:
            print("MONITOR ERROR:", e)

        time.sleep(get_check_interval())


# =========================
# MAIN
# =========================
def main():
    if not BEARER_TOKEN or not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing required environment variables.")

    init_db()

    t1 = threading.Thread(target=poll_telegram, daemon=True)
    t2 = threading.Thread(target=monitor_loop, daemon=True)

    t1.start()
    t2.start()

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
