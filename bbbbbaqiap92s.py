import os
import re
import asyncio
import requests
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import RetryAfter

# -------- تنظیمات اولیه (خواندن از متغیرهای محیطی) --------
# برای امنیت، این مقادیر را در هاست خود (مثلاً Render) به عنوان Environment Variables تنظیم کنید
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
CONSUMER_KEY = os.environ.get("CONSUMER_KEY")
CONSUMER_SECRET = os.environ.get("CONSUMER_SECRET")
API_URL = os.environ.get("API_URL") # مثلا: "https://yourdomain.com/wp-json/wc/v3/products"
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME") # مثلا: "@mychannel"

# نام فایلی که شناسه‌های محصولات ارسال شده را ذخیره می‌کند
SENT_IDS_FILE = "sent_products.txt"

# ساخت یک شیء از بات تلگرام
bot = Bot(token=TELEGRAM_TOKEN)


# -------- توابع ذخیره‌سازی و خواندن شناسه‌ها --------
def load_sent_ids() -> set:
    """شناسه‌های محصولات ارسال شده را از فایل می‌خواند و در یک set برمی‌گرداند."""
    try:
        with open(SENT_IDS_FILE, "r") as f:
            # خواندن هر خط، تبدیل به عدد صحیح و حذف فاصله‌های اضافی
            return {int(line.strip()) for line in f if line.strip()}
    except FileNotFoundError:
        # اگر فایل وجود نداشت، یک مجموعه خالی برگردان
        print("ℹ️ فایل sent_products.txt یافت نشد. یک فایل جدید ایجاد خواهد شد.")
        return set()

def save_sent_id(product_id: int):
    """شناسه‌ی یک محصول ارسال شده را به فایل اضافه می‌کند."""
    with open(SENT_IDS_FILE, "a") as f:
        f.write(f"{product_id}\n")


# -------- توابع کمکی (بدون تغییر) --------
def clean_html(raw_html: str) -> str:
    return re.sub(r"<.*?>", "", raw_html)

def format_tags(tags_list):
    tags = []
    for t in tags_list:
        tag_text = t["name"].replace(" ", "_")
        if "." not in tag_text:
            tags.append("#" + tag_text)
    return " ".join(tags)

async def send_to_channel(text: str, image: str = None, link: str = "#") -> bool:
    """پیام را به کانال تلگرام ارسال می‌کند و وضعیت موفقیت را برمی‌گرداند."""
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛍 خرید از سایت", url=link)]]
    )
    try:
        if image:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        else:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        print(f"✅ محصول با موفقیت به کانال ارسال شد.")
        return True
    except RetryAfter as e:
        print(f"⏳ تلگرام درخواست تاخیر داد. در حال انتظار برای {e.retry_after} ثانیه...")
        await asyncio.sleep(e.retry_after)
        return await send_to_channel(text, image, link) # تلاش مجدد برای ارسال
    except Exception as e:
        print(f"❌ خطا در ارسال به تلگرام: {e}")
        return False


# -------- منطق اصلی برنامه --------
async def check_for_new_products():
    """محصولات جدید را بررسی و ارسال می‌کند."""
    print("🔄 در حال بررسی محصولات جدید...")
    sent_ids = load_sent_ids()

    # دریافت ۱۰۰ محصول آخر که در سایت اضافه شده‌اند
    params = {
        "per_page": 100,
        "orderby": "date", # مرتب‌سازی بر اساس تاریخ
        "order": "desc"    # از جدید به قدیم
    }
    try:
        r = requests.get(API_URL, auth=(CONSUMER_KEY, CONSUMER_SECRET), params=params, timeout=20)
        r.raise_for_status()
        products = r.json()
    except requests.exceptions.RequestException as e:
        print(f"❌ خطا در دریافت اطلاعات از ووکامرس: {e}")
        return

    # پیدا کردن محصولاتی که قبلاً ارسال نشده‌اند
    new_products = [p for p in products if p["id"] not in sent_ids]

    if not new_products:
        print("✅ محصول جدیدی یافت نشد.")
        return

    print(f"🔥 {len(new_products)} محصول جدید یافت شد. در حال آماده‌سازی برای ارسال...")

    # محصولات جدید را از قدیمی‌ترین به جدیدترین ارسال می‌کنیم تا ترتیب رعایت شود
    for product in reversed(new_products):
        title = product["name"]
        print(f"➡️ در حال آماده‌سازی محصول: {title}")

        excerpt = clean_html(product.get("short_description", ""))
        image = product["images"][0]["src"] if product["images"] else None
        tags = format_tags(product.get("tags", []))
        link = product.get("permalink", "#")

        text = (
            f"🛒 <b>{title}</b>\n\n"
            f"{excerpt}\n\n"
            f"{tags}\n\n"
            f"{CHANNEL_USERNAME}"
        )

        # ارسال محصول و در صورت موفقیت، ذخیره شناسه آن
        if await send_to_channel(text, image, link):
            save_sent_id(product["id"])
            print(f"📝 شناسه محصول {product['id']} ذخیره شد.")
        
        # تاخیر بین هر ارسال برای جلوگیری از اسپم شدن
        await asyncio.sleep(3)

async def main():
    """حلقه اصلی که برنامه را به صورت دائمی اجرا می‌کند."""
    print("🚀 ربات شروع به کار کرد. برای توقف Ctrl+C را فشار دهید.")
    while True:
        try:
            await check_for_new_products()
        except Exception as e:
            print(f"❌ یک خطای پیش‌بینی نشده در حلقه اصلی رخ داد: {e}")
        
        # زمان انتظار بین هر بار بررسی (به ثانیه)
        # 3600 ثانیه = 1 ساعت
        check_interval_seconds = 3600
        print(f"😴 در حال استراحت به مدت {check_interval_seconds // 60} دقیقه...")
        await asyncio.sleep(check_interval_seconds)

if __name__ == "__main__":
    asyncio.run(main())
