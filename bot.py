#!/usr/bin/env python3
"""
Telegram bot that asks users for Hertz or Digital Ocean tokens on start.
Stored tokens can be used later (e.g. for API calls).
"""

import logging
import os

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Conversation states
CHOOSE_PROVIDER, AWAIT_TOKEN = range(2)

# Callback data for inline buttons
PROVIDER_HERTZ = "provider_hertz"
PROVIDER_DIGITAL_OCEAN = "provider_digitalocean"

# In-memory store: user_id -> {"provider": str, "token": str}
# Use this in your code to get the token for API calls
user_tokens: dict[int, dict[str, str]] = {}


def get_user_token(user_id: int) -> dict[str, str] | None:
    """Return stored provider and token for a user, or None."""
    return user_tokens.get(user_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send welcome message and provider choice on /start."""
    user = update.effective_user
    logger.info("User %s (%s) started the bot.", user.first_name, user.id)

    keyboard = [
        [
            InlineKeyboardButton("Hertz", callback_data=PROVIDER_HERTZ),
            InlineKeyboardButton("Digital Ocean", callback_data=PROVIDER_DIGITAL_OCEAN),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Welcome! Choose which provider you want to connect:\n\n"
        "• **Hertz** – enter your Hertz API token\n"
        "• **Digital Ocean** – enter your Digital Ocean API token",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )
    return CHOOSE_PROVIDER


async def provider_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle provider selection and ask for token."""
    query = update.callback_query
    await query.answer()

    provider = query.data
    if provider == PROVIDER_HERTZ:
        provider_name = "Hertz"
    else:
        provider_name = "Digital Ocean"

    context.user_data["provider"] = provider
    context.user_data["provider_name"] = provider_name

    await query.edit_message_text(
        f"You selected **{provider_name}**.\n\n"
        "Please send your API token in the next message.\n"
        "_Your token is stored only in this session and used for your requests._",
        parse_mode="Markdown",
    )
    return AWAIT_TOKEN


async def receive_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the token and confirm."""
    user_id = update.effective_user.id
    token = (update.message.text or "").strip()

    if not token:
        await update.message.reply_text("Please send a non-empty token.")
        return AWAIT_TOKEN

    provider = context.user_data.get("provider", "unknown")
    provider_name = context.user_data.get("provider_name", "Unknown")

    user_tokens[user_id] = {
        "provider": provider,
        "provider_name": provider_name,
        "token": token,
    }

    # Clear conversation state
    context.user_data.pop("provider", None)
    context.user_data.pop("provider_name", None)

    await update.message.reply_text(
        f"✅ **{provider_name}** token saved.\n\n"
        "You can use /start again to switch provider or update your token.",
        parse_mode="Markdown",
    )
    logger.info("User %s saved %s token.", user_id, provider_name)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the flow on /cancel."""
    await update.message.reply_text("Cancelled. Send /start to try again.")
    return ConversationHandler.END


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env or environment.")

    application = (
        Application.builder()
        .token(token)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_PROVIDER: [
                CallbackQueryHandler(provider_chosen, pattern=f"^({PROVIDER_HERTZ}|{PROVIDER_DIGITAL_OCEAN})$"),
            ],
            AWAIT_TOKEN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_token),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
