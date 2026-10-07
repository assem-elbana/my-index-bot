import os
import logging
import sqlite3
import asyncio
from datetime import datetime
from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)
from telegram.error import BadRequest
import google.generativeai as genai
import tempfile
import json

# ─── إعدادات ───────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "ضع_التوكن_هنا")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "ضع_الـ_API_KEY_هنا")
DB_PATH = "index.db"
MAX_PINNED_LENGTH = 3500  # حد أمان قبل تيليجرام (4096 max)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")

# ─── قاعدة البيانات ────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # جدول الإدخالات
    c.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            message_id INTEGER,
            date TEXT,
            type TEXT,
            content_preview TEXT,
            title TEXT,
            hashtags TEXT,
            message_link TEXT
        )
    """)

    # جدول صفحات الفهرس المثبتة
    c.execute("""
        CREATE TABLE IF NOT EXISTS index_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            page_number INTEGER,
            pinned_message_id INTEGER,
            is_current INTEGER DEFAULT 1
        )
    """)

    # جدول المحادثات مع الـ AI
    c.execute("""
        CREATE TABLE IF NOT EXISTS ai_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            timestamp TEXT
        )
    """)

    conn.commit()
    conn.close()

def save_entry(chat_id, message_id, date, msg_type, content_preview, title, hashtags, message_link):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO entries (chat_id, message_id, date, type, content_preview, title, hashtags, message_link)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (chat_id, message_id, date, msg_type, content_preview, title, hashtags, message_link))
    conn.commit()
    conn.close()

def get_all_entries(chat_id, limit=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    query = "SELECT date, type, title, hashtags, message_link FROM entries WHERE chat_id = ? ORDER BY id DESC"
    if limit:
        query += f" LIMIT {limit}"
    c.execute(query, (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def search_entries(chat_id, keyword):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT date, type, title, hashtags, message_link
        FROM entries
        WHERE chat_id = ? AND (title LIKE ? OR hashtags LIKE ? OR content_preview LIKE ?)
        ORDER BY id DESC
    """, (chat_id, f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"))
    rows = c.fetchall()
    conn.close()
    return rows

def get_stats(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM entries WHERE chat_id = ?", (chat_id,))
    total = c.fetchone()[0]
    c.execute("SELECT type, COUNT(*) FROM entries WHERE chat_id = ? GROUP BY type", (chat_id,))
    by_type = c.fetchall()
    conn.close()
    return total, by_type

def get_current_page(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT page_number, pinned_message_id FROM index_pages
        WHERE chat_id = ? AND is_current = 1
        ORDER BY page_number DESC LIMIT 1
    """, (chat_id,))
    row = c.fetchone()
    conn.close()
    return row  # (page_number, pinned_message_id) or None

def save_page(chat_id, page_number, pinned_message_id, is_current=1):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # إلغاء الصفحة الحالية لو كانت موجودة ولو بنعمل صفحة جديدة
    if is_current:
        c.execute("UPDATE index_pages SET is_current = 0 WHERE chat_id = ?", (chat_id,))
    c.execute("""
        INSERT INTO index_pages (chat_id, page_number, pinned_message_id, is_current)
        VALUES (?, ?, ?, ?)
    """, (chat_id, page_number, pinned_message_id, is_current))
    conn.commit()
    conn.close()

def update_page_message_id(chat_id, page_number, new_message_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        UPDATE index_pages SET pinned_message_id = ?
        WHERE chat_id = ? AND page_number = ?
    """, (new_message_id, chat_id, page_number))
    conn.commit()
    conn.close()

def save_ai_message(chat_id, user_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO ai_conversations (chat_id, user_id, role, content, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, (chat_id, user_id, role, content, datetime.now().isoformat()))
    # احتفظ بآخر 20 رسالة فقط لكل محادثة
    c.execute("""
        DELETE FROM ai_conversations WHERE id NOT IN (
            SELECT id FROM ai_conversations
            WHERE chat_id = ? AND user_id = ?
            ORDER BY id DESC LIMIT 20
        ) AND chat_id = ? AND user_id = ?
    """, (chat_id, user_id, chat_id, user_id))
    conn.commit()
    conn.close()

def get_ai_history(chat_id, user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT role, content FROM ai_conversations
        WHERE chat_id = ? AND user_id = ?
        ORDER BY id ASC
    """, (chat_id, user_id))
    rows = c.fetchall()
    conn.close()
    return rows

# ─── الذكاء الاصطناعي ──────────────────────────────────────────────────────
def generate_title_and_tags(text: str) -> tuple[str, str]:
    prompt = f"""أنت مساعد لتنظيم الأفكار والملاحظات الشخصية.
بناءً على النص التالي، أنشئ:
1. عنواناً مختصراً وواضحاً (لا يتجاوز 8 كلمات)
2. من 2 إلى 4 هاشتاقات ذات صلة باللغة العربية (بدون مسافات، تبدأ بـ #)

النص:
{text[:2000]}

أجب بهذا الشكل فقط (سطرين):
العنوان: [العنوان هنا]
الهاشتاقات: #هاشتاق1 #هاشتاق2 #هاشتاق3"""

    try:
        response = model.generate_content(prompt)
        lines = response.text.strip().split("\n")
        title = ""
        hashtags = ""
        for line in lines:
            if line.startswith("العنوان:"):
                title = line.replace("العنوان:", "").strip()
            elif line.startswith("الهاشتاقات:"):
                hashtags = line.replace("الهاشتاقات:", "").strip()
        return title or "بدون عنوان", hashtags or "#ملاحظة"
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return "بدون عنوان", "#ملاحظة"

def transcribe_voice(file_path: str) -> str:
    try:
        audio_file = genai.upload_file(file_path, mime_type="audio/ogg")
        response = model.generate_content([
            "استمع لهذا الصوت وحوّله إلى نص بالعربية بدقة، أعد النص فقط بدون أي تعليق:",
            audio_file
        ])
        return response.text.strip()
    except Exception as e:
        logger.error(f"Voice transcription error: {e}")
        return ""

def ai_chat(chat_id: int, user_id: int, user_message: str, index_context: str = "") -> str:
    """محادثة AI مع سياق الفهرس"""
    history = get_ai_history(chat_id, user_id)

    system_prompt = f"""أنت مساعد شخصي ذكي اسمك "فهرست". أنت تساعد صاحبك في تنظيم أفكاره وملاحظاته.
لديك وصول إلى فهرس أفكاره وملاحظاته.

سياق الفهرس الحالي:
{index_context if index_context else "الفهرس فارغ أو لم يُحمَّل بعد"}

تحدث بالعربية دائماً. كن ودوداً ومفيداً ومختصراً في إجاباتك."""

    # بناء تاريخ المحادثة
    chat_history = []
    for role, content in history:
        chat_history.append({
            "role": role,
            "parts": [content]
        })

    try:
        chat = model.start_chat(history=chat_history)
        response = chat.send_message(f"{system_prompt}\n\nالمستخدم: {user_message}")
        return response.text
    except Exception as e:
        logger.error(f"AI chat error: {e}")
        return "عذراً، حصل خطأ. جرّب تاني."

def build_message_link(chat_id: int, message_id: int) -> str:
    if str(chat_id).startswith("-100"):
        clean_id = str(chat_id)[4:]
        return f"https://t.me/c/{clean_id}/{message_id}"
    return ""

# ─── إدارة المنشور الثابت ──────────────────────────────────────────────────
def build_index_text(entries, page_num=1, next_page_link=None, prev_page_link=None) -> str:
    """بناء نص الفهرس المثبت"""
    lines = [f"📚 *فهرس الأفكار والملاحظات* — صفحة {page_num}\n"]
    lines.append("─" * 25 + "\n")

    for date, etype, title, hashtags, link in entries:
        icon = {"نص": "📝", "صوتية": "🎙️", "مقال/رابط": "🔗"}.get(etype, "📌")
        short_date = date[:10] if date else ""
        if link:
            lines.append(f"{icon} [{title}]({link})")
        else:
            lines.append(f"{icon} *{title}*")
        lines.append(f"   {hashtags} `{short_date}`\n")

    if prev_page_link:
        lines.append(f"\n⬅️ [الصفحة السابقة]({prev_page_link})")
    if next_page_link:
        lines.append(f"\n➡️ [الصفحة التالية]({next_page_link})")

    lines.append(f"\n─────\n_آخر تحديث: {datetime.now().strftime('%Y-%m-%d %H:%M')}_")
    return "\n".join(lines)

async def update_pinned_index(context, chat_id: int, new_entry: dict = None):
    """تحديث المنشور المثبت بالفهرس"""
    try:
        page_info = get_current_page(chat_id)
        all_entries = get_all_entries(chat_id)

        if not all_entries:
            return

        if page_info is None:
            # أول مرة: إنشاء منشور الفهرس
            text = build_index_text(all_entries[:30], page_num=1)
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
            await context.bot.pin_chat_message(chat_id=chat_id, message_id=sent.message_id, disable_notification=True)
            save_page(chat_id, 1, sent.message_id)
            return

        page_num, pinned_msg_id = page_info

        # بناء نص الفهرس للصفحة الحالية
        entries_per_page = 30
        start_idx = 0
        text = build_index_text(all_entries[:entries_per_page], page_num=page_num)

        if len(text) <= MAX_PINNED_LENGTH:
            # التحديث عادي
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=pinned_msg_id,
                    text=text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
            except BadRequest as e:
                logger.warning(f"Couldn't edit pinned message: {e}")
        else:
            # الصفحة امتلأت! إنشاء صفحة جديدة
            new_page_num = page_num + 1
            new_text = build_index_text(all_entries[:entries_per_page], page_num=new_page_num)
            new_sent = await context.bot.send_message(
                chat_id=chat_id,
                text=new_text,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )

            # رابط الصفحة الجديدة
            new_link = build_message_link(chat_id, new_sent.message_id)

            # تحديث الصفحة القديمة برابط الجديدة
            old_text = build_index_text(
                all_entries[entries_per_page:entries_per_page * 2],
                page_num=page_num,
                next_page_link=new_link
            )
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=pinned_msg_id,
                    text=old_text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
            except:
                pass

            # تثبيت الصفحة الجديدة
            await context.bot.pin_chat_message(chat_id=chat_id, message_id=new_sent.message_id, disable_notification=True)
            save_page(chat_id, new_page_num, new_sent.message_id)

    except Exception as e:
        logger.error(f"Error updating pinned index: {e}")

# ─── معالجة الرسائل ────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.message or update.channel_post
    if not msg or not msg.text:
        return

    if msg.text.startswith("/"):
        return

    bot_username = context.bot.username
    chat_id = msg.chat_id
    message_id = msg.message_id

    # ─── محادثة AI: لو ذكر البوت أو رد عليه ───────────────────────────────
    is_mentioned = bot_username and f"@{bot_username}" in (msg.text or "")
    is_reply_to_bot = (
        msg.reply_to_message and
        msg.reply_to_message.from_user and
        msg.reply_to_message.from_user.username == bot_username
    )

    if is_mentioned or is_reply_to_bot:
        user_text = msg.text.replace(f"@{bot_username}", "").strip()
        user_id = msg.from_user.id if msg.from_user else 0

        # جلب سياق الفهرس للـ AI
        entries = get_all_entries(chat_id, limit=50)
        index_summary = "\n".join([f"- {title} {hashtags} ({date[:10]})" for date, _, title, hashtags, _ in entries[:20]])

        save_ai_message(chat_id, user_id, "user", user_text)
        reply_text = ai_chat(chat_id, user_id, user_text, index_summary)
        save_ai_message(chat_id, user_id, "model", reply_text)

        await msg.reply_text(reply_text, parse_mode="Markdown")
        return

    # ─── فهرسة الرسالة ────────────────────────────────────────────────────
    if len(msg.text) < 20:
        return

    date = datetime.now().strftime("%Y-%m-%d %H:%M")
    link = build_message_link(chat_id, message_id)

    title, hashtags = generate_title_and_tags(msg.text)
    save_entry(chat_id, message_id, date, "نص", msg.text[:300], title, hashtags, link)

    reply = f"📌 *{title}*\n{hashtags}"
    if link:
        reply += f"\n[🔗 الرسالة]({link})"
    await msg.reply_text(reply, parse_mode="Markdown", disable_web_page_preview=True)

    # تحديث الفهرس المثبت
    await update_pinned_index(context, chat_id)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.message or update.channel_post
    if not msg:
        return

    voice = msg.voice or msg.audio
    if not voice:
        return

    chat_id = msg.chat_id
    message_id = msg.message_id
    date = datetime.now().strftime("%Y-%m-%d %H:%M")
    link = build_message_link(chat_id, message_id)

    processing_msg = await msg.reply_text("🎙️ بحوّل الصوت لنص...")

    file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    await file.download_to_drive(tmp_path)

    text = transcribe_voice(tmp_path)
    os.unlink(tmp_path)

    if not text:
        await processing_msg.edit_text("❌ مقدرتش أفهم الصوت، جرب تاني.")
        return

    title, hashtags = generate_title_and_tags(text)
    save_entry(chat_id, message_id, date, "صوتية", text[:300], title, hashtags, link)

    reply = f"🎙️ *{title}*\n{hashtags}\n\n📝 _{text[:200]}_"
    if link:
        reply += f"\n[🔗 الرسالة]({link})"

    await processing_msg.edit_text(reply, parse_mode="Markdown", disable_web_page_preview=True)
    await update_pinned_index(context, chat_id)

async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الرسائل المعاد توجيهها للاستيراد"""
    msg: Message = update.message
    if not msg or not msg.forward_date:
        return

    # تحقق إنها رسالة معاد توجيهها
    chat_id = msg.chat_id
    message_id = msg.message_id
    date = datetime.fromtimestamp(msg.forward_date.timestamp()).strftime("%Y-%m-%d %H:%M")

    content = msg.text or msg.caption or ""
    if not content and not msg.voice:
        return

    if msg.voice:
        processing_msg = await msg.reply_text("🎙️ بستورد الصوت...")
        file = await context.bot.get_file(msg.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await file.download_to_drive(tmp_path)
        content = transcribe_voice(tmp_path)
        os.unlink(tmp_path)
        if not content:
            await processing_msg.edit_text("❌ مقدرتش أستورد الصوت.")
            return
        msg_type = "صوتية"
    else:
        msg_type = "نص"
        if len(content) < 10:
            return

    link = build_message_link(chat_id, message_id)
    title, hashtags = generate_title_and_tags(content)
    save_entry(chat_id, message_id, date, msg_type, content[:300], title, hashtags, link)

    reply = f"📥 *تم الاستيراد:*\n{title}\n{hashtags}"
    await msg.reply_text(reply, parse_mode="Markdown")
    await update_pinned_index(context, chat_id)

# ─── الأوامر ───────────────────────────────────────────────────────────────
async def cmd_index(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    chat_id = msg.chat_id
    entries = get_all_entries(chat_id)

    if not entries:
        await msg.reply_text("📂 الفهرس فاضي لسه! ابدأ اكتب أفكارك.")
        return

    lines = ["📚 *فهرس أفكارك:*\n"]
    for date, etype, title, hashtags, link in entries[:40]:
        icon = {"نص": "📝", "صوتية": "🎙️", "مقال/رابط": "🔗"}.get(etype, "📌")
        short_date = date[:10] if date else ""
        if link:
            lines.append(f"{icon} [{title}]({link}) — {hashtags} `{short_date}`")
        else:
            lines.append(f"{icon} *{title}* — {hashtags} `{short_date}`")

    if len(entries) > 40:
        lines.append(f"\n_... و {len(entries) - 40} إدخالات أخرى. استخدم /بحث للبحث_")

    text = "\n".join(lines)
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        await msg.reply_text(chunk, parse_mode="Markdown", disable_web_page_preview=True)

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    chat_id = msg.chat_id

    if not context.args:
        await msg.reply_text("🔍 مثال: `/بحث كتب`", parse_mode="Markdown")
        return

    keyword = " ".join(context.args)
    entries = search_entries(chat_id, keyword)

    if not entries:
        await msg.reply_text(f"🔍 مفيش نتائج لـ *{keyword}*", parse_mode="Markdown")
        return

    lines = [f"🔍 *نتائج \"{keyword}\":*\n"]
    for date, etype, title, hashtags, link in entries[:20]:
        icon = {"نص": "📝", "صوتية": "🎙️", "مقال/رابط": "🔗"}.get(etype, "📌")
        if link:
            lines.append(f"{icon} [{title}]({link})\n   {hashtags} | _{date[:10]}_\n")
        else:
            lines.append(f"{icon} *{title}*\n   {hashtags} | _{date[:10]}_\n")

    await msg.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    chat_id = msg.chat_id
    total, by_type = get_stats(chat_id)

    lines = [f"📊 *إحصائياتك:*\n", f"📌 الإجمالي: *{total}* إدخال\n"]
    for etype, count in by_type:
        icon = {"نص": "📝", "صوتية": "🎙️", "مقال/رابط": "🔗"}.get(etype, "📌")
        lines.append(f"{icon} {etype}: {count}")

    await msg.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_refresh_index(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إعادة بناء الفهرس المثبت"""
    msg = update.message or update.channel_post
    chat_id = msg.chat_id
    await msg.reply_text("🔄 بعمل فهرس جديد...")
    await update_pinned_index(context, chat_id)
    await msg.reply_text("✅ تم تحديث الفهرس المثبت!")

async def cmd_import_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """وضع الاستيراد للرسائل القديمة"""
    msg = update.message
    help_text = """📥 *وضع استيراد الرسائل القديمة:*

ببساطة **أعد توجيه** (Forward) أي رسالة قديمة للجروب ده وهيفهرسها.

أو لو عندك كتير، صدّر المحادثة من تيليجرام كـ JSON وبعتهولي وأنا هعمل استيراد شامل."""
    await msg.reply_text(help_text, parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    bot_username = context.bot.username
    help_text = f"""🤖 *أنا فهرست — مساعدك الشخصي*

*الفهرسة التلقائية:*
📝 الرسائل النصية (+20 حرف)
🎙️ الرسائل الصوتية
🔗 الروابط والمقالات

*الأوامر:*
📚 `/فهرس` — عرض الفهرس
🔍 `/بحث [كلمة]` — البحث
📊 `/احصاء` — الإحصائيات
🔄 `/تحديث` — تحديث المنشور الثابت
📥 `/استيراد` — استيراد رسائل قديمة
❓ `/مساعدة` — هذه الرسالة

*المساعد الذكي:*
منشن البوت `@{bot_username}` أو رد على رسالة منه واسأله أي حاجة!"""
    await msg.reply_text(help_text, parse_mode="Markdown")

# ─── التشغيل ───────────────────────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # الأوامر
    app.add_handler(CommandHandler(["فهرس", "index"], cmd_index))
    app.add_handler(CommandHandler(["بحث", "search"], cmd_search))
    app.add_handler(CommandHandler(["احصاء", "stats"], cmd_stats))
    app.add_handler(CommandHandler(["تحديث", "refresh"], cmd_refresh_index))
    app.add_handler(CommandHandler(["استيراد", "import"], cmd_import_mode))
    app.add_handler(CommandHandler(["مساعدة", "help", "start"], cmd_help))

    # الرسائل الصوتية
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

    # الرسائل المعاد توجيهها (للاستيراد)
    app.add_handler(MessageHandler(filters.FORWARDED, handle_forwarded))

    # الرسائل النصية (يشمل الـ AI والفهرسة)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("فهرست شغّال! 🚀")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
