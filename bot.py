#!/usr/bin/env python3
"""
Telegram bot that interacts with the Hope VPN API.
"""

import logging
import os
import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
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

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# API Configuration
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:3000")

# Conversation states
SELECT_PROVIDER, ENTER_TOKEN, MAIN_MENU, SERVER_DETAILS, ACCOUNT_CONFIRM = range(5)

# Callback data prefixes
CB_PROVIDER = "prov_"
CB_SERVER = "srv_"
CB_CREATE = "create_server"
CB_REFRESH = "refresh"
CB_DELETE = "del_"
CB_MANAGE = "manage_"
CB_REFRESH_SERVER = "refresh_srv_"
CB_CHECK = "check_"
CB_DELETE_ACCOUNT = "delete_account"
CB_CONFIRM_DELETE_ACCOUNT = "confirm_delete_account"
CB_CANCEL_DELETE_ACCOUNT = "cancel_delete_account"
CB_BACK = "back_main"
CB_TOKEN_CANCEL = "token_cancel"

# Tutorial Links
TUTORIALS = {
    "digitalocean": "https://docs.digitalocean.com/reference/api/create-personal-access-token/",
    "linode": "https://www.linode.com/docs/guides/getting-started-with-the-linode-api/#get-an-access-token",
    "hetzner": "https://docs.hetzner.com/cloud/api/getting-started/generating-api-token/",
}


async def api_request(method: str, endpoint: str, json=None, params=None, timeout=5.0):
    """Helper to make async API requests. Uses short timeout so we fail fast."""
    url = f"{API_BASE_URL.rstrip('/')}{endpoint}"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.request(method, url, json=json, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"API %s %s -> %s: %s", method, url, e.response.status_code, e.response.text)
            return None
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            logger.error(f"API %s %s failed: %s", method, url, e)
            return None
        except Exception as e:
            logger.error(f"API Request Failed %s %s: %s", method, url, e)
            return None


def _parse_error_message(response: httpx.Response) -> str:
    """Extract error message from API error response."""
    try:
        data = response.json()
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
    except Exception:
        pass
    if response.text:
        return response.text[:200]
    return f"HTTP {response.status_code}"


async def api_request_with_error(method: str, endpoint: str, json=None, params=None, timeout=30.0):
    """Like api_request but returns (data, error_message). Use for flows that should show API errors."""
    url = f"{API_BASE_URL.rstrip('/')}{endpoint}"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.request(method, url, json=json, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json(), None
        except httpx.HTTPStatusError as e:
            logger.error(f"API %s %s -> %s: %s", method, url, e.response.status_code, e.response.text)
            return None, _parse_error_message(e.response)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            logger.error(f"API %s %s failed: %s", method, url, e)
            return None, "Could not reach the API. Check it is running."
        except Exception as e:
            logger.error(f"API Request Failed %s %s: %s", method, url, e)
            return None, str(e) or "Request failed"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: Sign up user and show providers."""
    user = update.effective_user
    logger.info("User %s (%s) started the bot.", user.first_name, user.id)

    # 1. Terms
    status_msg = await update.message.reply_text(
        "👋 Welcome to Hope VPN Bot!\n\n"
        "By continuing, you agree to our Terms of Service.\n\n"
        "⏳ _Connecting to services..._",
        parse_mode="Markdown"
    )

    # 2. Signup
    try:
        signup_data = await api_request("POST", "/signup", json={"telegramId": str(user.id)})
    except Exception as e:
        logger.error(f"Signup exception: {e}")
        signup_data = None
        
    user_id = signup_data.get("userId") if signup_data else None
    if not user_id:
        err_text = (
            "👋 Welcome to Hope VPN Bot!\n\n"
            "⚠️ **Error**: Could not connect to the VPN API Server.\n"
            f"Check that the API is running (e.g. `{API_BASE_URL}`).\n"
            "Try /start again when the server is ready."
        )
        try:
            await status_msg.edit_text(err_text, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(err_text, parse_mode="Markdown")
        return ConversationHandler.END

    context.user_data["user_id"] = user_id
    context.user_data["telegram_id"] = str(user.id)

    # 3. Check if user already has a selection setup
    try:
        user_info = await api_request("GET", "/user", params={"userId": str(user_id)})
        # API returns normalized "selections" array
        selections = (user_info or {}).get("selections") or (user_info or {}).get("Selections") or []
        if user_info and len(selections) > 0:
            # User already has a provider configured – keep journey state in bot
            first_sel = selections[0]
            provider = (first_sel.get("Provider") or first_sel.get("provider")) or {}
            context.user_data["provider"] = provider.get("name") or "unknown"
            await status_msg.delete()
            return await show_main_menu(update, context)
        # If user exists but has no selection, still show servers (if any)
        if user_info:
            servers = await api_request("GET", "/servers", params={"userId": str(user_id)})
            if servers:
                await status_msg.delete()
                return await show_main_menu(update, context)
    except Exception as e:
        logger.error(f"Error fetching user info: {e}")
        # Continue to provider selection even if user fetch fails (assuming new user flow)

    # 4. Get Providers
    providers = await api_request("GET", "/providers")
    if not providers:
        await status_msg.edit_text(
            "⚠️ **Error**: Could not fetch providers.\n"
            "The API might be reachable but returning empty data."
        )
        return ConversationHandler.END

    # One provider per row
    keyboard = []
    for p in providers:
        if p.get("isActive", True):
            btn = InlineKeyboardButton(p["name"].title(), callback_data=f"{CB_PROVIDER}{p['name']}")
            keyboard.append([btn])

    await status_msg.edit_text(
        "Please select a provider to host your VPN server:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_PROVIDER

async def handle_provider_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save provider choice and ask for token."""
    query = update.callback_query
    await query.answer()

    provider_name = query.data.replace(CB_PROVIDER, "")
    context.user_data["provider"] = provider_name
    
    tutorial_link = TUTORIALS.get(provider_name.lower(), "https://google.com")
    
    msg = (
        f"You selected **{provider_name.title()}**.\n\n"
        "1. Please generate an API Token for your account (use the button below for a tutorial).\n\n"
        "2. ⚠️ **IMPORTANT**: Make sure you have added funds/billing info to your account, "
        "otherwise the server creation will fail.\n\n"
        "3. Paste your API Token in your next message.\n\n"
        "_You can cancel and pick another provider with the button below, or send /cancel or /start to start over._"
    )
    
    tutorial_btn = InlineKeyboardButton("📖 Click here for a tutorial", url=tutorial_link)
    cancel_btn = InlineKeyboardButton("◀️ Cancel – pick another provider", callback_data=CB_TOKEN_CANCEL)
    await query.edit_message_text(
        msg,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[tutorial_btn], [cancel_btn]])
    )
    return ENTER_TOKEN

async def handle_token_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User cancelled token step – show provider selection again."""
    query = update.callback_query
    await query.answer()
    providers = await api_request("GET", "/providers")
    if not providers:
        await query.edit_message_text("Could not load providers. Try /start again.")
        return ConversationHandler.END
    keyboard = []
    for p in providers:
        if p.get("isActive", True):
            keyboard.append([InlineKeyboardButton(p["name"].title(), callback_data=f"{CB_PROVIDER}{p['name']}")])
    await query.edit_message_text(
        "Please select a provider to host your VPN server:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_PROVIDER

async def handle_token_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate token and save selection."""
    token = update.message.text.strip()
    user_id = context.user_data.get("user_id")
    provider = context.user_data.get("provider")

    if not token:
        await update.message.reply_text("Token cannot be empty.")
        return ENTER_TOKEN

    # Submit Selection to API
    payload = {
        "userId": user_id,
        "token": token,
        "provider": provider
    }
    
    resp = await api_request("POST", "/selections", json=payload)
    if not resp:
        await update.message.reply_text("❌ Failed to verify/save your token. Please try again or /start over.")
        return ENTER_TOKEN

    await update.message.reply_text("✅ Token verified successfully!")
    return await show_main_menu(update, context)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display user's servers and options."""
    # Determine if we are editing a message or sending a new one
    is_callback = bool(update.callback_query)
    message_func = update.callback_query.edit_message_text if is_callback else update.message.reply_text
    
    user_id = context.user_data.get("user_id")
    if not user_id:
        text = "⚠️ Session lost. Send /start to begin."
        if is_callback:
            await message_func(text)
        else:
            await message_func(text)
        return ConversationHandler.END

    # Fetch servers
    servers = await api_request("GET", "/servers", params={"userId": str(user_id)})
    if servers is None:
        text = "⚠️ Error fetching servers. Check the API is running and try /start again."
        if is_callback:
            await message_func(text)
        else:
            await message_func(text)
        return MAIN_MENU

    server_count = len(servers)
    text = f"🖥 **Your Servers** ({server_count})\n"
    text += "Status guide: 🟡 provisioning • 🟢 active • 🔴 error\n\n"
    
    keyboard = []
    
    if not servers:
        text += "You don't have any servers yet."
    else:
        for s in servers:
            status = s.get("status", "unknown")
            ip = s.get("ipAddress") or "No IP"
            label = s.get("label") or f"Server {s['id'][:8]}"
            vpn_status = s.get("vpnInstallStatus")
            vpn_msg = s.get("vpnInstallMessage")
            badge = "🟡"
            if status in ("active", "running"):
                badge = "🟢"
            elif status in ("error", "failed"):
                badge = "🔴"
            
            # Buttons for each server
            btn_text = f"{badge} {label} ({status})"
            manage_btn = InlineKeyboardButton(f"ℹ️ More Info", callback_data=f"{CB_MANAGE}{s['id']}")
            refresh_btn = InlineKeyboardButton("🔄 Refresh", callback_data=f"{CB_REFRESH_SERVER}{s['id']}")
            delete_btn = InlineKeyboardButton("🗑 Delete", callback_data=f"{CB_DELETE}{s['id']}")
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"{CB_SERVER}{s['id']}")])
            keyboard.append([manage_btn, refresh_btn, delete_btn])
            
            vpn_text = f" | VPN: {vpn_status}" if vpn_status else ""
            vpn_note = f" ({vpn_msg})" if vpn_msg else ""
            text += f"• {badge} `{ip}` - **{status}**{vpn_text}{vpn_note}\n"

    # Actions
    actions_row = [
        InlineKeyboardButton("➕ Create Server", callback_data=CB_CREATE),
        InlineKeyboardButton("🔄 Refresh", callback_data=CB_REFRESH),
    ]
    keyboard.append(actions_row)
    keyboard.append([InlineKeyboardButton("🧹 Remove Account & Token", callback_data=CB_DELETE_ACCOUNT)])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        if is_callback:
            await message_func(text, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await message_func(text, reply_markup=reply_markup, parse_mode="Markdown")
    except BadRequest as e:
        if "not modified" not in (e.message or "").lower():
            raise
        # Message already has same content/keyboard – ignore
    return MAIN_MENU

async def handle_main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle clicks on Main Menu."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = context.user_data.get("user_id")

    if data == CB_REFRESH:
        return await show_main_menu(update, context)
        
    elif data == CB_CREATE:
        if not user_id:
            await query.message.reply_text("⚠️ Session lost. Send /start to begin.")
            return ConversationHandler.END
        # Always rely on API for selection/token state
        await query.edit_message_text(
            "🚀 **Creating your server**\n\n"
            "Step 1/3: Generating SSH keys\n"
            "Step 2/3: Registering keys with provider\n"
            "Step 3/3: Provisioning VM\n\n"
            "_This can take 30–90 seconds. We'll refresh your list when it's ready._",
            parse_mode="Markdown",
        )
        resp, err = await api_request_with_error(
            "POST", "/servers/create", json={"userId": user_id}, timeout=60.0
        )
        if not resp:
            msg = (
                "❌ **Failed to create server**\n\n"
                f"_{err or 'Check your token/funds.'}_\n\n"
                "If this keeps happening, run /start and re-save your provider token."
            )
            await query.message.reply_text(msg, parse_mode="Markdown")
        return await show_main_menu(update, context)

    elif data.startswith(CB_SERVER):
        server_id = data.replace(CB_SERVER, "")
        context.user_data["selected_server_id"] = server_id
        return await show_server_details(update, context)
        
    elif data.startswith(CB_MANAGE):
        server_id = data.replace(CB_MANAGE, "")
        context.user_data["selected_server_id"] = server_id
        return await show_server_details(update, context)

    elif data.startswith(CB_REFRESH_SERVER):
        server_id = data.replace(CB_REFRESH_SERVER, "")
        await query.edit_message_text("🔄 Refreshing server status...")
        await api_request("GET", f"/servers/{server_id}", params={"userId": str(user_id)})
        return await show_main_menu(update, context)

    elif data.startswith(CB_DELETE):
        server_id = data.replace(CB_DELETE, "")
        await query.edit_message_text("🗑 Deleting server...")
        await api_request("DELETE", f"/servers/{server_id}", json={"userId": user_id})
        await query.message.reply_text("✅ Server deleted.")
        return await show_main_menu(update, context)

    elif data == CB_DELETE_ACCOUNT:
        warning = (
            "⚠️ **Delete account & token**\n\n"
            "This will:\n"
            "• Remove your account\n"
            "• Delete saved provider token\n"
            "• Remove server records from the app\n\n"
            "_Provider servers are not deleted automatically._"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Yes, delete everything", callback_data=CB_CONFIRM_DELETE_ACCOUNT)],
                [InlineKeyboardButton("Cancel", callback_data=CB_CANCEL_DELETE_ACCOUNT)],
            ]
        )
        await query.edit_message_text(warning, reply_markup=keyboard, parse_mode="Markdown")
        return ACCOUNT_CONFIRM

    return MAIN_MENU

async def show_server_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show details for a specific server."""
    user_id = context.user_data.get("user_id")
    server_id = context.user_data.get("selected_server_id")
    
    # We could fetch fresh details, or pass them. Let's fetch.
    server = await api_request("GET", f"/servers/{server_id}", params={"userId": user_id})
    
    if not server:
        await update.callback_query.edit_message_text("Server not found.")
        return await show_main_menu(update, context)
        
    # Format details
    status = server.get("status", "unknown")
    ip = server.get("ipAddress") or "Pending..."
    vpn_status = server.get("vpnInstallStatus") or "unknown"
    vpn_msg = server.get("vpnInstallMessage")
    
    vpn_note = f"**VPN Note:** {vpn_msg}\n" if vpn_msg else ""
    text = (
        "ℹ️ **Server Details**\n\n"
        f"**ID:** `{server['id']}`\n"
        f"**IP:** `{ip}`\n"
        f"**Status:** {status}\n"
        f"**VPN Install:** {vpn_status}\n"
        f"{vpn_note}\n"
        # In a real app, maybe show SSH keys or connection string here
    )
    
    keyboard = [
        [InlineKeyboardButton("✅ Health Check", callback_data=f"{CB_CHECK}{server_id}")],
        [InlineKeyboardButton("🗑 Delete Server", callback_data=f"{CB_DELETE}{server_id}")],
        [InlineKeyboardButton("🔙 Back to List", callback_data=CB_BACK)]
    ]
    
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return SERVER_DETAILS

async def handle_server_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle actions in Server Details view."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = context.user_data.get("user_id")
    
    if data == CB_BACK:
        return await show_main_menu(update, context)
        
    elif data.startswith(CB_DELETE):
        server_id = data.replace(CB_DELETE, "")
        await query.edit_message_text("🗑 Deleting server...")
        
        await api_request("DELETE", f"/servers/{server_id}", json={"userId": user_id})
        await query.message.reply_text("✅ Server deleted.")
        
        return await show_main_menu(update, context)

    elif data.startswith(CB_CHECK):
        server_id = data.replace(CB_CHECK, "")
        await query.edit_message_text("🩺 Running health check...")
        result = await api_request("GET", f"/servers/{server_id}/check", params={"userId": str(user_id)})
        if not result:
            await query.message.reply_text("⚠️ Health check failed. Try again.")
            return await show_server_details(update, context)
        health = result.get("health") or {}
        text = (
            "🩺 **Health Check**\n\n"
            f"**Ping:** {'✅' if health.get('ping') else '❌'}\n"
            f"**SSH:** {'✅' if health.get('ssh') else '❌'}\n"
            f"**Iran Accessible:** {'✅' if health.get('iranAccessible') else '❌'}\n"
        )
        msg = health.get("message")
        if msg:
            text += f"\n**Note:** {msg}\n"
        await query.message.reply_text(text, parse_mode="Markdown")
        return await show_server_details(update, context)
        
    return SERVER_DETAILS

async def handle_account_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle account deletion confirmation."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = context.user_data.get("user_id")

    if data == CB_CANCEL_DELETE_ACCOUNT:
        return await show_main_menu(update, context)

    if data == CB_CONFIRM_DELETE_ACCOUNT:
        await query.edit_message_text("🧹 Deleting your account and token...")
        resp, err = await api_request_with_error(
            "DELETE", "/user", json={"userId": user_id}, timeout=20.0
        )
        if resp:
            context.user_data.clear()
            await query.message.reply_text("✅ Your data was deleted. Send /start to begin again.")
            return ConversationHandler.END
        else:
            await query.message.reply_text(
                f"❌ Failed to delete account.\n\n_{err or 'Please try again.'}_",
                parse_mode="Markdown",
            )
            return await show_main_menu(update, context)

    return MAIN_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel flow."""
    await update.message.reply_text("Cancelled. /start to restart.")
    return ConversationHandler.END

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        # For testing, we might not have a token set, so warn
        print("Warning: TELEGRAM_BOT_TOKEN not set.")
    
    # If no token is present, builder() will fail. 
    # But we assume user will provide it in .env
    if not token: 
         return

    application = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECT_PROVIDER: [
                CallbackQueryHandler(handle_provider_selection, pattern=f"^{CB_PROVIDER}")
            ],
            ENTER_TOKEN: [
                CallbackQueryHandler(handle_token_cancel, pattern=f"^{CB_TOKEN_CANCEL}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_token_input),
            ],
            MAIN_MENU: [
                CallbackQueryHandler(handle_main_menu_callback)
            ],
            SERVER_DETAILS: [
                CallbackQueryHandler(handle_server_details_callback)
            ],
            ACCOUNT_CONFIRM: [
                CallbackQueryHandler(handle_account_confirm_callback)
            ]
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("cancel", cancel),
        ],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    
    print(f"Bot is running... (API: {API_BASE_URL})")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
