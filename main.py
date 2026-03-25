import os
import re
import time
import json
import sqlite3
import hashlib
import threading
import subprocess
from datetime import datetime, timezone

import requests

# =========================
# ENV
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
CHECK_INTERVAL_DEFAULT = int(os.getenv("CHECK_INTERVAL", "15"))
CONTROL_CHAT_ID = os.getenv("CONTROL_CHAT_ID", "").strip()
ACCOUNTS_TEXT = os.getenv("ACCOUNTS_TEXT", "").strip()

DB_FILE = "data.db"
TWS_DB = "accounts.db"
ACCOUNTS_FILE = "accounts.txt"


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
    upsert_bot_state("accounts_hash", "")

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
            [{"text": "👥 حالة الحسابات"}, {"text": "🔄 إعادة تهيئة الحسابات"}],
            [{"text": "🔐 إعادة تسجيل الفاشلة"}, {"text": "🧪 اختبار الإرسال"}],
            [{"text": "🗑 مسح السجل"}, {"text": "❌ إلغاء"}]
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
# TWSCRAPE
# =========================
def run_cmd(cmd, timeout=180):
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        raise RuntimeError(stderr or stdout or "command failed")
    return result.stdout


def tws_cmd(*args):
    return ["twscrape", "--db", TWS_DB, *args]


def write_accounts_file():
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        f.write(ACCOUNTS_TEXT + "\n")


def accounts_hash():
    return hashlib.sha256(ACCOUNTS_TEXT.encode("utf-8")).hexdigest()


def bootstrap_accounts(force=False):
    if not ACCOUNTS_TEXT:
        return "لا توجد حسابات في ACCOUNTS_TEXT."

    new_hash = accounts_hash()
    old_hash = get_bot_state("accounts_hash", "")

    if (new_hash == old_hash) and not force:
        return "الحسابات مهيأة مسبقًا."

    write_accounts_file()

    out1 = run_cmd(
        tws_cmd(
            "add_accounts",
            ACCOUNTS_FILE,
            "username:password:email:email_password"
        ),
        timeout=300
    )
    print("ADD_ACCOUNTS OUTPUT:", out1)

    out2 = run_cmd(tws_cmd("login_accounts"), timeout=1200)
    print("LOGIN_ACCOUNTS OUTPUT:", out2)

    upsert_bot_state("accounts_hash", new_hash)
    return "تمت تهيئة الحسابات وتسجيل دخولها."


def relogin_failed_accounts():
    try:
        out = run_cmd(tws_cmd("relogin_failed"), timeout=1200)
        print("RELOGIN OUTPUT:", out)
        return out or "تم تنفيذ relogin_failed."
    except Exception as e:
        print("RELOGIN ERROR:", str(e))
        return f"RELOGIN ERROR: {str(e)}"


def get_accounts_status():
    out = run_cmd(tws_cmd("accounts"), timeout=120)
    print("ACCOUNTS STATUS OUTPUT:", out)
    return out.strip() or "لا توجد بيانات."


def parse_search_output(raw_text):
    raw_text = raw_text.strip()
    if not raw_text:
        return []

    items = []

    if raw_text.startswith("["):
        try:
            data = json.loads(raw_text)
            if isinstance(data, list):
                return data
        except Exception:
            pass

    for line in raw_text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            items.append(json.loads(line))
        except Exception:
            continue
    return items


def search_x_free(keyword, limit=10):
    query = f'({keyword}) filter:videos'
    out = run_cmd(tws_cmd("search", query, f"--limit={limit}"), timeout=240)
    print(f"SEARCH OUTPUT [{keyword}]:", out[:2000])
    return parse_search_output(out)


def get_tweet_id(item):
    for key in ("id", "tweetId", "rest_id"):
        val = item.get(key)
        if val:
            return str(val)
    return None


def get_tweet_text(item):
    for key in ("rawContent", "content", "full_text", "text"):
        val = item.get(key)
        if val:
            return str(val)
    return ""


def get_tweet_date(item):
    for key in ("date", "created_at", "createdAt"):
        val = item.get(key)
        if val:
            try:
                return parse_iso(str(val))
            except Exception:
                return None
    return None


def get_tweet_url(item):
    username = None
    if isinstance(item.get("user"), dict):
        username = item["user"].get("username") or item["user"].get("login")

    tweet_id = get_tweet_id(item)

    if username and tweet_id:
        return f"https://x.com/{username}/status/{tweet_id}"
    if tweet_id:
        return f"https://x.com/i/web/status/{tweet_id}"
    return ""


def extract_video_url(item):
    media = item.get("media") or {}
    possible_urls = []

    if isinstance(media, dict):
        videos = media.get("videos") or media.get("video")
        if isinstance(videos, list):
            for v in videos:
                if isinstance(v, dict):
                    u = v.get("url")
                    if u:
                        possible_urls.append(u)
        elif isinstance(videos, dict):
            u = videos.get("url")
            if u:
                possible_urls.append(u)

    raw = json.dumps(item, ensure_ascii=False)
    mp4s = re.findall(r'https?://[^\s"\\]+\.mp4[^\s"\\]*', raw)
    possible_urls.extend(mp4s)

    seen = set()
    unique = []
    for u in possible_urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)

    return unique[0] if unique else None


# =========================
# UI
# =========================
def status_text():
    rows = list_keywords()
    interval = get_check_interval()
    paused = "متوقف" if is_paused() else "يعمل"
    last_scan_at = get_bot_state("last_scan_at", "") or "غير متوفر"
    last_error = get_bot_state("last_error", "") or "لا يوجد"

    return (
        f"📊 حالة البوت\n\n"
        f"الحالة: {paused}\n"
        f"وقت الفحص: {interval} ثانية\n"
        f"عدد الكلمات: {len(rows)}\n"
        f"وجهة الإرسال: {CHAT_ID}\n"
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

    if text == "👥 حالة الحسابات":
        try:
            out = get_accounts_status()
            send_message(chat_id, f"👥 حالة الحسابات\n\n{out}", with_menu=True)
        except Exception as e:
            send_message(chat_id, f"فشل قراءة حالة الحسابات:\n{str(e)[:300]}", with_menu=True)
        return

    if text == "🔄 إعادة تهيئة الحسابات":
        try:
            msg = bootstrap_accounts(force=True)
            send_message(chat_id, msg, with_menu=True)
        except Exception as e:
            send_message(chat_id, f"فشل تهيئة الحسابات:\n{str(e)[:300]}", with_menu=True)
        return

    if text == "🔐 إعادة تسجيل الفاشلة":
        try:
            out = relogin_failed_accounts()
            send_message(chat_id, f"{out[:3500]}", with_menu=True)
        except Exception as e:
            send_message(chat_id, f"فشل إعادة التسجيل:\n{str(e)[:300]}", with_menu=True)
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

    rows = list_keywords()
    if not rows:
        upsert_bot_state("last_scan_at", now_iso())
        return

    for row in rows:
        keyword = row["keyword"]
        activated_at = parse_iso(row["activated_at"])

        try:
            tweets = search_x_free(keyword, limit=10)
            tweets = list(reversed(tweets))

            for item in tweets:
                post_id = get_tweet_id(item)
                if not post_id:
                    continue
                if already_sent(post_id):
                    continue

                tweet_date = get_tweet_date(item)
                if tweet_date and tweet_date < activated_at:
                    continue

                text = get_tweet_text(item)
                post_url = get_tweet_url(item)
                caption = f"{text}\n\n{post_url}\n\nKeyword: {keyword}".strip()

                video_url = extract_video_url(item)

                if video_url:
                    try:
                        result = send_target_video(video_url, caption)
                        if result.get("ok"):
                            mark_sent(post_id, keyword)
                            continue
                    except Exception as send_err:
                        print("VIDEO SEND ERROR:", send_err)

                msg_result = send_target_message(caption)
                if msg_result.get("ok"):
                    mark_sent(post_id, keyword)

        except Exception as e:
            upsert_bot_state("last_error", f"TWS [{keyword}]: {str(e)[:180]}")
            print(f"TWS ERROR [{keyword}]:", e)

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

    try:
        msg = bootstrap_accounts(force=False)
        print(msg)
    except Exception as e:
        upsert_bot_state("last_error", f"BOOTSTRAP: {str(e)[:180]}")
        print("BOOTSTRAP ERROR:", e)

    t1 = threading.Thread(target=poll_telegram, daemon=True)
    t2 = threading.Thread(target=monitor_loop, daemon=True)
    t1.start()
    t2.start()

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
