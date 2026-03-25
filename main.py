import os
os.system("python -m playwright install chromium")

import re
import time
import json
import sqlite3
import threading
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from playwright.sync_api import sync_playwright

# =========================
# ENV
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
CHECK_INTERVAL_DEFAULT = int(os.getenv("CHECK_INTERVAL", "15"))
CONTROL_CHAT_ID = os.getenv("CONTROL_CHAT_ID", "").strip()

DB_FILE = "data.db"
STATE_FILE = "x_state.json"


# =========================
# DB
# =========================
def db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(dt_str: str):
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


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

    upsert_bot_state("check_interval", str(CHECK_INTERVAL_DEFAULT))
    upsert_bot_state("is_paused", "0")
    upsert_bot_state("update_offset", "0")
    upsert_bot_state("total_sent", "0")
    upsert_bot_state("last_scan_at", "")
    upsert_bot_state("last_error", "")

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


def already_sent(post_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sent_posts WHERE post_id = ?", (post_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def mark_sent(post_id, keyword):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO sent_posts (post_id, keyword, sent_at)
        VALUES (?, ?, ?)
    """, (post_id, keyword, now_iso()))
    conn.commit()
    conn.close()

    total_sent = int(get_bot_state("total_sent", "0"))
    upsert_bot_state("total_sent", str(total_sent + 1))


def clear_sent_posts():
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sent_posts")
    conn.commit()
    conn.close()


def get_update_offset():
    return int(get_bot_state("update_offset", "0"))


def set_update_offset(offset):
    upsert_bot_state("update_offset", str(offset))


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
    params = {"timeout": 30, "offset": get_update_offset()}
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def main_menu_keyboard():
    keyboard = {
        "keyboard": [
            [{"text": "➕ إضافة كلمة"}, {"text": "➖ حذف كلمة"}],
            [{"text": "📋 عرض الكلمات"}, {"text": "⏱ تغيير وقت الفحص"}],
            [{"text": "▶️ تشغيل الرصد"}, {"text": "⏸ إيقاف الرصد"}],
            [{"text": "📊 الحالة"}, {"text": "📈 الإحصائيات"}],
            [{"text": "🧪 اختبار الإرسال"}, {"text": "🗑 مسح السجل"}],
            [{"text": "❌ إلغاء"}]
        ],
        "resize_keyboard": True,
        "is_persistent": True
    }
    return json.dumps(keyboard, ensure_ascii=False)


def send_message(chat_id, text, with_menu=False):
    data = {"chat_id": str(chat_id), "text": text[:4096]}
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
# PLAYWRIGHT / X
# =========================
def login_x_and_save_state():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=120000)
        print("سجّل الدخول يدويًا داخل المتصفح، ثم ارجع للتيرمنال واضغط Enter...")
        input()
        context.storage_state(path=STATE_FILE)
        browser.close()
        print(f"تم حفظ الجلسة في {STATE_FILE}")


def search_x_with_playwright(keyword, limit=10):
    if not os.path.exists(STATE_FILE):
        raise RuntimeError("ملف x_state.json غير موجود")

    results = []
    query = f'({keyword}) filter:videos'
    search_url = f"https://x.com/search?q={quote(query)}&src=typed_query&f=live"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=STATE_FILE,
            viewport={"width": 1280, "height": 2200}
        )
        page = context.new_page()
        page.goto(search_url, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(5000)

        for _ in range(3):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(2500)

        articles = page.locator("article")
        count = min(articles.count(), limit)

        for i in range(count):
            article = articles.nth(i)

            try:
                links = article.locator('a[href*="/status/"]')
                if links.count() == 0:
                    continue

                href = links.first.get_attribute("href")
                if not href or "/status/" not in href:
                    continue

                full_url = "https://x.com" + href
                post_id_match = re.search(r"/status/(\d+)", href)
                if not post_id_match:
                    continue
                post_id = post_id_match.group(1)

                text = ""
                text_nodes = article.locator('div[lang]')
                if text_nodes.count() > 0:
                    parts = []
                    for j in range(text_nodes.count()):
                        t = text_nodes.nth(j).inner_text().strip()
                        if t:
                            parts.append(t)
                    text = "\n".join(parts).strip()

                created_at = None
                time_node = article.locator("time")
                if time_node.count() > 0:
                    created_at = time_node.first.get_attribute("datetime")

                video_url = None
                html = article.inner_html()
                mp4s = re.findall(r'https://video\.twimg\.com/[^"\']+\.mp4[^"\']*', html)
                if mp4s:
                    video_url = mp4s[0].replace("&amp;", "&")

                results.append({
                    "id": post_id,
                    "text": text or "منشور جديد مطابق للكلمة",
                    "url": full_url,
                    "video_url": video_url,
                    "date": created_at or now_iso()
                })

            except Exception as e:
                print("ARTICLE PARSE ERROR:", e)

        browser.close()

    return results


# =========================
# UI
# =========================
def status_text():
    rows = list_keywords()
    interval = get_check_interval()
    paused = "متوقف" if is_paused() else "يعمل"
    last_scan_at = get_bot_state("last_scan_at", "") or "غير متوفر"
    last_error = get_bot_state("last_error", "") or "لا يوجد"
    login_state = "موجودة" if os.path.exists(STATE_FILE) else "غير موجودة"

    return (
        f"📊 حالة البوت\n\n"
        f"الحالة: {paused}\n"
        f"وقت الفحص: {interval} ثانية\n"
        f"عدد الكلمات: {len(rows)}\n"
        f"وجهة الإرسال: {CHAT_ID}\n"
        f"جلسة X: {login_state}\n"
        f"آخر فحص: {last_scan_at}\n"
        f"آخر خطأ: {last_error}"
    )


def stats_text():
    rows = list_keywords()
    total_sent = get_bot_state("total_sent", "0")
    return (
        f"📈 الإحصائيات\n\n"
        f"عدد الكلمات المحفوظة: {len(rows)}\n"
        f"إجمالي المنشورات المرسلة: {total_sent}\n"
        f"وقت الفحص الحالي: {get_check_interval()} ثانية"
    )


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
            send_message(chat_id, "أرسل رقمًا فقط. مثال: 5", with_menu=True)
            return

        seconds = int(text)
        if seconds < 5:
            send_message(chat_id, "الحد الأدنى هنا 5 ثوانٍ.", with_menu=True)
            return

        upsert_bot_state("check_interval", str(seconds))
        clear_user_pending_action(chat_id)
        send_message(chat_id, f"تم تغيير وقت الفحص إلى {seconds} ثانية.", with_menu=True)
        return


def handle_menu_text(chat_id, text):
    text = (text or "").strip()

    if text == "/start":
        clear_user_pending_action(chat_id)
        send_message(chat_id, "مرحبًا بك.\nاختر من الأزرار بالأسفل:", with_menu=True)
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
        send_message(chat_id, "أرسل الوقت بالثواني. مثال: 10", with_menu=True)
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

    if text == "📈 الإحصائيات":
        send_message(chat_id, stats_text(), with_menu=True)
        return

    if text == "🧪 اختبار الإرسال":
        send_target_message("✅ اختبار ناجح: البوت يرسل بشكل صحيح.")
        send_message(chat_id, "تم إرسال رسالة اختبار.", with_menu=True)
        return

    if text == "🗑 مسح السجل":
        clear_sent_posts()
        send_message(chat_id, "تم مسح سجل المنشورات المرسلة.", with_menu=True)
        return

    if text == "❌ إلغاء" or text == "/cancel":
        clear_user_pending_action(chat_id)
        send_message(chat_id, "تم إلغاء العملية.", with_menu=True)
        return

    pending = get_user_pending_action(chat_id)
    if pending:
        handle_pending_action(chat_id, pending, text)
        return

    send_message(chat_id, "اختر من الأزرار بالأسفل.", with_menu=True)


# =========================
# LOOPS
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
            upsert_bot_state("last_error", f"TELEGRAM: {str(e)[:180]}")
            print("TELEGRAM ERROR:", e)

        time.sleep(2)


def process_keywords():
    if is_paused():
        return

    if not os.path.exists(STATE_FILE):
        upsert_bot_state("last_error", "ملف x_state.json غير موجود")
        upsert_bot_state("last_scan_at", now_iso())
        return

    rows = list_keywords()
    if not rows:
        upsert_bot_state("last_scan_at", now_iso())
        return

    for row in rows:
        keyword = row["keyword"]
        activated_at = parse_iso(row["activated_at"])

        try:
            tweets = search_x_with_playwright(keyword, limit=10)

            for item in reversed(tweets):
                post_id = item["id"]
                if already_sent(post_id):
                    continue

                tweet_date_str = item.get("date")
                if not tweet_date_str:
                    continue

                tweet_date = parse_iso(tweet_date_str)
                if tweet_date <= activated_at:
                    continue

                caption = (
                    f"{item['text']}\n\n"
                    f"رابط المنشور:\n{item['url']}\n\n"
                    f"Keyword: {keyword}"
                )

                if item.get("video_url"):
                    try:
                        result = send_target_video(item["video_url"], caption)
                        if result.get("ok"):
                            mark_sent(post_id, keyword)
                            continue
                    except Exception as send_err:
                        print("VIDEO SEND ERROR:", send_err)

                fallback_text = (
                    f"{item['text']}\n\n"
                    f"رابط المنشور:\n{item['url']}\n\n"
                    f"رابط الفيديو المباشر:\n{item.get('video_url', 'غير متوفر')}\n\n"
                    f"Keyword: {keyword}"
                )
                msg_result = send_target_message(fallback_text)
                if msg_result.get("ok"):
                    mark_sent(post_id, keyword)

        except Exception as e:
            upsert_bot_state("last_error", f"X [{keyword}]: {str(e)[:180]}")
            print(f"X ERROR [{keyword}]:", e)

    upsert_bot_state("last_scan_at", now_iso())


def monitor_loop():
    while True:
        try:
            process_keywords()
        except Exception as e:
            upsert_bot_state("last_error", f"MONITOR: {str(e)[:180]}")
            print("MONITOR ERROR:", e)

        time.sleep(get_check_interval())


# =========================
# MAIN
# =========================
def main():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing required environment variables.")

    init_db()

    t1 = threading.Thread(target=poll_telegram, daemon=True)
    t2 = threading.Thread(target=monitor_loop, daemon=True)
    t1.start()
    t2.start()

    while True:
        time.sleep(60)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        login_x_and_save_state()
    else:
        main()
