import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# Gemini client import (from Google Gen AI SDK)
# pip package name can be google-genai or google (depending on SDK version). Example from docs uses: from google import genai
try:
    from google import genai
except Exception:
    # fallback import name some docs use
    import google
    genai = google.genai

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELE_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
if not TELE_TOKEN:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN env var")
if not GEMINI_KEY:
    raise SystemExit("Set GEMINI_API_KEY env var")

# Initialize Gemini client
client = genai.Client(api_key=GEMINI_KEY)

# Report categories and short prompts
REPORT_CATS = {
    "spam": "Spam (unwanted messages, repetitive links, scams).",
    "harassment": "Harassment / Threats (abusive language, threats).",
    "illegal": "Illegal content (copyright infringement, illegal sale).",
    "unofficial_apps": "Use of unofficial apps (modified clients, deceptive apps).",
    "malware": "Sending malicious content / malware (links, attachments intended to harm)."
}

# Helper: keyboard builders
def main_menu_kb():
    kb = [
        [InlineKeyboardButton("Whatsapp ban", callback_data="menu_whatsapp")],
        [InlineKeyboardButton("Home", callback_data="home")]
    ]
    return InlineKeyboardMarkup(kb)

def whatsapp_menu_kb():
    kb = [
        [InlineKeyboardButton("Spam report", callback_data="cat:spam")],
        [InlineKeyboardButton("Harassment / Threat", callback_data="cat:harassment")],
        [InlineKeyboardButton("Illegal content", callback_data="cat:illegal")],
        [InlineKeyboardButton("Use of unofficial apps", callback_data="cat:unofficial_apps")],
        [InlineKeyboardButton("Malicious content / Malware", callback_data="cat:malware")],
        [InlineKeyboardButton("Back", callback_data="home"), InlineKeyboardButton("Home", callback_data="home")]
    ]
    return InlineKeyboardMarkup(kb)

def action_kb(can_regen=True):
    kb = []
    if can_regen:
        kb.append([InlineKeyboardButton("Regenerate", callback_data="regen")])
    kb.append([InlineKeyboardButton("Back", callback_data="menu_whatsapp"), InlineKeyboardButton("Home", callback_data="home")])
    return InlineKeyboardMarkup(kb)

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Hi — I generate WhatsApp report / appeal message templates. "
        "Use the menu below to choose the report type. All generated outputs are plain message text you can copy to WhatsApp."
    )
    await update.message.reply_text(text, reply_markup=main_menu_kb())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /start to open the menu. Use buttons to generate report messages.")

# Callback for all inline buttons
async def button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "home":
        await query.edit_message_text("Main menu:", reply_markup=main_menu_kb())
        return

    if data == "menu_whatsapp":
        await query.edit_message_text("Please select a button below:", reply_markup=whatsapp_menu_kb())
        return

    # Category selected
    if data.startswith("cat:"):
        cat = data.split(":", 1)[1]
        # store selected category in user_data so regen knows what to call
        context.user_data["last_cat"] = cat
        # generate initial message
        await query.edit_message_text(f"Generating a {REPORT_CATS.get(cat,cat)} message...")

        msg = await generate_report_message(cat, context)
        if not msg:
            await query.edit_message_text("Failed to generate message. Try Regenerate or Back.", reply_markup=action_kb(can_regen=True))
            return

        # send generated message with action buttons (Regenerate / Back / Home)
        await query.edit_message_text(msg, reply_markup=action_kb(can_regen=True))

    # Regenerate button pressed
    if data == "regen":
        cat = context.user_data.get("last_cat")
        if not cat:
            await query.edit_message_text("No category to regenerate for. Go back to menu.", reply_markup=main_menu_kb())
            return
        await query.edit_message_text(f"Regenerating a {REPORT_CATS.get(cat,cat)} message...")
        msg = await generate_report_message(cat, context)
        if not msg:
            await query.edit_message_text("Failed to generate message. Try again.", reply_markup=action_kb(can_regen=True))
            return
        await query.edit_message_text(msg, reply_markup=action_kb(can_regen=True))

# Gemini call - asynchronous wrapper
async def generate_report_message(category: str, context):
    """
    Calls Gemini 2.5 Flash to generate a single report message.
    IMPORTANT: provide a strict prompt instructing the model to output ONLY the message text (no extra commentary).
    """
    prompts = {
        "spam": "Generate a short, formal WhatsApp report message (1-3 sentences) reporting spam from another WhatsApp number. Output only the message — nothing else.",
        "harassment": "Generate a short, formal WhatsApp report message (1-3 sentences) reporting harassment or threats from another WhatsApp number. Output only the message — nothing else.",
        "illegal": "Generate a short, formal WhatsApp report message (1-3 sentences) reporting illegal content or activity from another WhatsApp number. Output only the message — nothing else.",
        "unofficial_apps": "Generate a short, formal WhatsApp report message (1-3 sentences) reporting use of an unofficial or modified WhatsApp client by a user. Output only the message — nothing else.",
        "malware": "Generate a short, formal WhatsApp report message (1-3 sentences) reporting receiving malicious files or links from another WhatsApp number. Output only the message — nothing else."
    }

    prompt = prompts.get(category, f"Generate a short WhatsApp report message (1-3 sentences) about: {category}. Output only the message — nothing else.")

    try:
        # Use the Python client per Gemini docs
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        # response.text or response.output_text depending on client version
        text = getattr(response, "text", None) or getattr(response, "output_text", None) or str(response)
        # Trim whitespace and return
        return text.strip()
    except Exception as e:
        logger.exception("Gemini call failed: %s", e)
        return None

# Fallback text handler (not used for generation; keeps bot responsive)
async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use the menu buttons. /start to open main menu.", reply_markup=main_menu_kb())

def main():
    app = ApplicationBuilder().token(TELE_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(button_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
