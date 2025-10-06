# bot.py
import os
import re
import json
import asyncio
import requests
import threading
from typing import Set

from flask import Flask, Response
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import RetryAfter

# -------- تنظیمات از متغیرهای محیطی --------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")          # توکن ربات
CHANNEL_ID = os.environ.get("CHANNEL_ID")                  # مثل -100123456789
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "")  # مثل @MyChannel
CONSUMER_KEY = os.environ.get("CONSUMER_KEY")              # ووکامرس
CONSUMER_SECRET = os.environ.get("CONSUMER_SECRET")
API_URL = os.environ.get("API_URL")                        # https://example.com/wp-json/wc/v3/products

# پارامترهای قابل تنظیم
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "3600"))  # هر چند ثانیه بررسی شود (پیشفرض 1 ساعت)
SENT_IDS_FILE = os.environ.get("SENT_IDS_FILE", "sent_products.txt")  # فایل ذخیره‌ی شناسه‌ها
SEND_DELAY_SECONDS = float(os.environ.get("SEND_DELAY_SECONDS", "3"))  # تأخیر بین ارسال هر پست (ثانیه)

# بررسی اولیه متغیرهای محیطی
required = {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "CHANNEL_ID": CHANNEL_ID,
    "CONSUMER_KEY": CONSUMER_KEY,
    "CONSUMER_SECRET": CONSUMER_SECRET,
    "API_URL": API_URL,
}
missing = [k for k,v in required.items() if not v]
if missing:
    print("❌ WARNING: متغیرهای محیطی زیر تنظیم نشده‌اند:", missing)
    # برنامه ادامه می‌دهد اما بدون اینها کار نخواهد کرد.

# ساخت بات تلگرام
bot = Bot(token=TELEGRAM_TOKEN)

# -------- وب‌سرور ساده برای پینگ --------
app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    # پاسخ ساده برای پینگ (UptimeRobot/cron-job)
    return Response("OK", status=200, mimetype="text/plain")

# -------- توابع کمکی --------
def clean_html(raw_html: str) -> str:
    """حذف تگ‌های HTML"""
    if not raw_html:
        return ""
    # حذف تگ‌ها و چند جای خالی اضافه
    text = re.sub(r"<.*?>", "", raw_html)
    text = re.sub(r"\s+\n", "\n", text)
    text = text.strip()
    return text

def format_tags(tags_list) -> str:
    """ساخت هشتگ بدون تگ‌هایی که نقطه دارند"""
    tags = []
    for t in tags_list:
        name = str(t.get("name","")).strip()
        if not name:
            continue
        tag_text = name.replace(" ", "_")
        if "." in tag_text:
            continue
        tags.append("#" + tag_text)
    return " ".join(tags)

def load_sent_ids() -> Set[int]:
    """خواندن شناسه‌های ارسال‌شده از فایل"""
    try:
        with open(SENT_IDS_FILE, "r", encoding="utf-8") as f:
            return {int(line.strip()) for line in f if line.strip()}
    except FileNotFoundError:
        print("ℹ️ فایل sent_products.txt وجود ندارد — ایجاد خواهد شد.")
        return set()
    except Exception as e:
        print("❌ خطا در خواندن فایل شناسه‌ها:", e)
        return set()

def save_sent_id(product_id: int):
    """افزودن شناسه جدید به فایل (append)"""
    try:
        with open(SENT_IDS_FILE, "a", encoding="utf-8") as f:
            f.write(f"{product_id}\n")
    except Exception as e:
        print("❌ خطا در ذخیره شناسه:", e)

async def send_to_channel(text: str, image: str = None, link: str = "#") -> bool:
    """ارسال پیام با دکمه شیشه‌ای؛ در صورت موفقیت True برمی‌گرداند."""
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🛍 خرید از سایت", url=link)]])
    try:
        if image:
            await bot.send_photo(chat_id=CHANNEL_ID, photo=image, caption=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        print("✅ ارسال شد.")
        return True
    except RetryAfter as e:
        # اگر تلگرام محدودیت اعمال کرد، صبر کن و دوباره تلاش کن
        print(f"⏳ RetryAfter: صبر به مدت {e.retry_after} ثانیه...")
        await asyncio.sleep(e.retry_after)
        return await send_to_channel(text, image, link)
    except Exception as e:
        print("❌ خطا در ارسال به تلگرام:", type(e).__name__, e)
        return False

# -------- منطق بررسی و ارسال محصولات --------
async def fetch_products(per_page=100):
    """دریافت محصولات (در یک thread جدا با requests)"""
    params = {"per_page": per_page, "orderby": "date", "order": "asc"}  # به صورت قدیمی->جدید
    try:
        # از asyncio.to_thread استفاده می‌کنیم تا درخواست blocking، لوپ اصلی رو مسدود نکنه
        resp = await asyncio.to_thread(requests.get, API_URL, {"auth": (CONSUMER_KEY, CONSUMER_SECRET), "params": params})
        # NOTE: بعضی هاست‌ها اجازه ارسال auth tuple از طریق requests.get(..., auth=(...)) دارند.
        # اگر above کار نکرد، می‌توانید URL را با consumer keys به صورت پارامتر اضافه کنید (نه امن).
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print("❌ خطا در fetch_products:", e)
        return []

async def check_for_new_products_once():
    """یکبار محصولات را چک کرده و ارسال می‌کند (از قدیمی به جدید)"""
    print("🔄 در حال بررسی محصولات جدید...")
    sent_ids = load_sent_ids()
    products = await fetch_products(per_page=100)  # می‌توان مقدار را افزایش داد در صورت نیاز

    if not isinstance(products, list):
        print("❌ پاسخ API غیرمنتظره است:", products)
        return

    # فیلتر محصولات جدید (آنهایی که قبلاً ارسال نشده‌اند)
    new_products = [p for p in products if int(p.get("id", 0)) not in sent_ids]

    if not new_products:
        print("✅ محصول جدیدی یافت نشد.")
        return

    print(f"🔥 {len(new_products)} محصول جدید یافت شد. ارسال از قدیمی‌ترین به جدیدترین...")

    # چون fetch با order=asc گرفت، new_products به ترتیب قدیمی->جدید خواهد بود.
    for product in new_products:
        pid = int(product.get("id", 0))
        title = product.get("name", "بدون عنوان")
        print(f"➡️ آماده‌سازی ارسال: {title} (id={pid})")

        excerpt = clean_html(product.get("short_description", "") or "")
        image = None
        try:
            image = product.get("images", [])[0].get("src") if product.get("images") else None
        except Exception:
            image = None

        tags = format_tags(product.get("tags", []))
        link = product.get("permalink", "#")

        text = (
            f"🛒 <b>{title}</b>\n\n"
            f"{excerpt}\n\n"
            f"{tags}\n\n"
            f"{CHANNEL_USERNAME}"
        )

        ok = await send_to_channel(text, image, link)
        if ok:
            save_sent_id(pid)
            print(f"📝 ذخیره شد: {pid}")
        else:
            print(f"⚠️ ارسال برای محصول {pid} موفق نبود — ادامه ارسال محصولات بعدی.")

        await asyncio.sleep(SEND_DELAY_SECONDS)

# حلقه اصلی پس‌زمینه
async def background_loop():
    print("⏳ حلقه پس‌زمینه شروع شد.")
    while True:
        try:
            await check_for_new_products_once()
        except Exception as e:
            print("❌ خطای غیرمنتظره در حلقه پس‌زمینه:", e)
        print(f"😴 استراحت برای {CHECK_INTERVAL_SECONDS} ثانیه...")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

# اجرای حلقه پس‌زمینه در یک thread جدا با asyncio.run
def start_background_loop_in_thread():
    def _run():
        asyncio.run(background_loop())
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    print("🧵 ترد پس‌زمینه استارت شد.")

# -------- نقطه ورود برنامه --------
if __name__ == "__main__":
    # فقط وقتی مستقیم اجرا می‌شود ترد پس‌زمینه را اجرا کن
    start_background_loop_in_thread()
    # سپس وب‌سرور Flask را اجرا می‌کنیم (در Render از Gunicorn استفاده کنید)
    # برای توسعه محلی:
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
