import os
import asyncio
import logging
from typing import Optional

import asyncpg
from google import genai  # google generative ai client
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    __version__ as ptb_version,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ========== config ==========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}

if not all([TELEGRAM_TOKEN, GEMINI_API_KEY, DATABASE_URL]):
    raise RuntimeError("Set TELEGRAM_TOKEN, GEMINI_API_KEY and DATABASE_URL env vars")

# report categories mapping (main menu -> subtypes)
REPORT_CATEGORIES = {
    "spam": "Spam report",
    "harassment": "Harassment/Threat",
    "illegal": "Illegal content",
    "unofficial": "Use of unofficial apps",
    "malware": "Sending malicious content / malware",
}

# logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# initialize genai client
genai.configure(api_key=GEMINI_API_KEY)


# ========== DB helpers ==========
CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS tg_users (
    id BIGINT PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    started_at TIMESTAMPTZ DEFAULT now()
);
"""

async def init_db(pool):
    async with pool.acquire() as conn:
        await conn.execute(CREATE_USERS_TABLE)


# ========== Gemini report generator ==========
async def generate_report_text(reason: str) -> str:
    """
    Call Gemini 2.5 Flash to generate a report message for the given reason.
    Return: generated text only.
    """
    prompt = (
        "You are a helper that generates a short, formal report message to appeal or report a WhatsApp account.\n"
        "You MUST ONLY output the report text (no extra commentary, no signatures, no 'As an AI' lines).\n"
        f"Reason: {reason}\n"
        "Include the necessary details for a WhatsApp report: what happened in 2-4 concise paragraphs,"
        " include time, example messages (short), and a clear request for action (e.g., reinstate/ban/remove content).\n"
        "Keep it professional and concise (max ~200-300 words)."
    )

    # Using genai client synchronous-like call (this client supports blocking calls; we wrap in executor)
    # NOTE: the client may be synchronous; run in thread to avoid blocking the event loop.
    loop = asyncio.get_running_loop()

    def sync_call():
        response = genai.Client().models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt],
            # You can tune temperature, max output tokens etc via additional args if needed.
        )
        # response.text is the aggregated text output
        return getattr(response, "text", str(response))

    text = await loop.run_in_executor(None, sync_call)
    # Ensure we only return text (strip)
    return text.strip()


# ========== Telegram UI helpers ==========
def main_menu_keyboard():
    buttons = [
        [InlineKeyboardButton(v, callback_data=f"cat:{k}")]
        for k, v in REPORT_CATEGORIES.items()
    ]
    # no Back on main menu, but include Home (redundant) and maybe Help
    buttons.append([InlineKeyboardButton("Help", callback_data="help")])
    return InlineKeyboardMarkup(buttons)


def report_item_keyboard(cat_key: str):
    kb = [
        [
            InlineKeyboardButton("Regenerate", callback_data=f"regen:{cat_key}"),
            InlineKeyboardButton("Back", callback_data="back:menu"),
        ],
        [InlineKeyboardButton("Home", callback_data="home")],
    ]
    return InlineKeyboardMarkup(kb)


# ========== Handlers ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # welcome + main menu
    text = (
        "Hi — this bot helps generate formal WhatsApp report & appeal messages.\n\n"
        "Choose the issue you want to report from the menu below. Each report button will generate a ready-to-send report message."
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())

    # save user to DB (async)
    pool = context.application.bot_data.get("db_pool")
    if pool:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tg_users (id, username, first_name, last_name) VALUES ($1,$2,$3,$4) ON CONFLICT (id) DO NOTHING",
                user.id,
                user.username,
                user.first_name,
                user.last_name,
            )


async def help_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # callback or message help
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(
            "Choose an issue from the menu. Regenerate will call the AI to produce another report (text only)."
        )
        await update.callback_query.message.reply_markup(main_menu_keyboard())
    else:
        await update.message.reply_text(
            "Choose an issue from the menu below.",
            reply_markup=main_menu_keyboard(),
        )


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    await q.answer()  # remove loading state quickly

    if data.startswith("cat:"):
        cat = data.split(":", 1)[1]
        reason = REPORT_CATEGORIES.get(cat, cat)
        # inform user generating...
        msg = await q.message.reply_text("Generating report message... ⏳")
        try:
            generated = await generate_report_text(reason)
        except Exception as e:
            log.exception("Gemini call failed")
            await msg.edit_text(f"Error generating report: {e}")
            return
        # edit the generating message to output the report and attach Regenerate/Back/Home
        await msg.edit_text(generated, reply_markup=report_item_keyboard(cat))

    elif data.startswith("regen:"):
        cat = data.split(":", 1)[1]
        reason = REPORT_CATEGORIES.get(cat, cat)
        # send generating indicator and replace
        msg = await q.message.reply_text("Regenerating report message... ⏳")
        try:
            generated = await generate_report_text(reason)
        except Exception as e:
            log.exception("Gemini call failed")
            await msg.edit_text(f"Error regenerating: {e}")
            return
        await msg.edit_text(generated, reply_markup=report_item_keyboard(cat))

    elif data == "back:menu":
        # show other reports (the main menu)
        await q.message.reply_text("Back to report menu:", reply_markup=main_menu_keyboard())

    elif data == "home":
        await q.message.reply_text("Main menu:", reply_markup=main_menu_keyboard())

    elif data == "help":
        await q.message.reply_text(
            "This bot generates report text only. Use Regenerate to get a new report, Back to return to report list, Home for main menu."
        )
    else:
        await q.message.reply_text("Unknown action.")


# Admin broadcast command
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("You are not authorized to broadcast.")
        return

    # args -> message to broadcast
    msg = " ".join(context.args).strip()
    if not msg:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    pool = context.application.bot_data.get("db_pool")
    if not pool:
        await update.message.reply_text("Database not ready.")
        return

    # fetch users
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM tg_users")
    user_ids = [r["id"] for r in rows]
    sent = 0
    failed = 0

    await update.message.reply_text(f"Broadcasting to {len(user_ids)} users...")
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"Done. Sent: {sent}, Failed: {failed}")


# minimal /status
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is running.")


# fallback message handler (just show main menu)
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use the menu:", reply_markup=main_menu_keyboard())


# ========== main ==========
async def main():
    # build app
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # init db pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    await init_db(pool)
    app.bot_data["db_pool"] = pool

    # handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(callback_query_handler))
    app.add_handler(CommandHandler("help", help_cb))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), message_handler))

    print("Bot starting...")
    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
