#!/usr/bin/env python3
"""
Telegram bot that interacts with the Hope VPN API.
"""

import logging
import os
from io import BytesIO
from typing import Optional, Tuple

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
CB_METRICS = "metrics_"
CB_VPN_VERIFY = "vpn_verify_"
CB_SSH_KEY = "ssh_key_"
CB_MANAGE_TOKENS = "manage_tokens"
CB_TOKEN_UPDATE = "token_update_"
CB_TOKEN_REMOVE = "token_remove_"
CB_TOKEN_REPLACE = "token_replace_"
CB_MANAGE_SERVERS = "manage_servers"
CB_ADD_PROVIDER = "add_provider"
CB_BACK_TOKENS = "back_tokens"
CB_DELETE_ACCOUNT = "delete_account"
CB_CONFIRM_DELETE_ACCOUNT = "confirm_delete_account"
CB_CANCEL_DELETE_ACCOUNT = "cancel_delete_account"
CB_BACK_DETAILS = "back_details"
CB_BACK = "back_main"
CB_TOKEN_CANCEL = "token_cancel"

# Vendor-neutral status display (API status -> emoji, label)
STATUS_READY = ("active", "running")
STATUS_ISSUE = ("error", "failed", "deleted")
STATUS_SETUP = ("creating", "provisioning", "booting", "pending", "starting")


def _status_display(api_status: str) -> Tuple[str, str]:
    """Return (emoji, label) for API status. Vendor-neutral."""
    s = (api_status or "").strip().lower()
    if s in STATUS_READY:
        return "✅", "Ready"
    if s in STATUS_ISSUE:
        return "❌", "Issue"
    if s in STATUS_SETUP:
        return "⏳", "Setting up"
    return "⚪", api_status or "Unknown"


def _vpn_ready_label(server: dict) -> Tuple[str, str]:
    """Return (emoji, label) for VPN readiness."""
    vpn_status = (server.get("vpnInstallStatus") or "").strip().lower()
    vpn_msg = (server.get("vpnInstallMessage") or "").strip().lower()
    api_status = (server.get("status") or "").strip().lower()

    if vpn_status in ("installed", "ready") or "verified" in vpn_msg:
        return "✅", "Ready"
    if vpn_status in ("installing", "pending", "booting", "creating", "provisioning") or api_status in STATUS_SETUP:
        return "⏳", "Loading"
    if vpn_status in ("failed", "error", "not_running") or api_status in STATUS_ISSUE:
        return "❌", "Error"
    return "⏳", "Loading"


def _parse_prometheus_metrics(raw: str) -> dict:
    """Parse Prometheus text into a dict of metric -> value (last sample)."""
    metrics = {}
    if not raw or not raw.strip():
        return metrics
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        name, rest = parts[0], parts[1]
        # Strip labels: name{label="x"} -> name
        if "{" in name:
            name = name.split("{", 1)[0]
        value_str = rest.split()[-1] if rest else ""
        try:
            metrics[name] = float(value_str)
        except ValueError:
            continue
    return metrics


def _format_bytes(num: float) -> str:
    if num is None:
        return "N/A"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    n = float(num)
    while n >= 1024 and idx < len(units) - 1:
        n /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(n)} {units[idx]}"
    return f"{n:.2f} {units[idx]}"


def _format_uptime(seconds: float) -> str:
    if seconds is None:
        return "N/A"
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, s = divmod(s, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _format_conduit_metrics(raw: str) -> str:
    metrics = _parse_prometheus_metrics(raw)
    if not metrics:
        return "No Conduit metrics in response (VPN may still be installing)."
    connected = metrics.get("conduit_connected_clients")
    up = metrics.get("conduit_bytes_uploaded")
    down = metrics.get("conduit_bytes_downloaded")
    uptime = metrics.get("conduit_uptime_seconds")
    lines = [
        f"Connected clients: {int(connected) if connected is not None else 'N/A'}",
        f"Uploaded: {_format_bytes(up) if up is not None else 'N/A'}",
        f"Downloaded: {_format_bytes(down) if down is not None else 'N/A'}",
        f"Uptime: {_format_uptime(uptime)}",
    ]
    return "\n".join(lines)


def _format_global_stats(stats: Optional[dict]) -> str:
    """Return formatted global stats lines for messages."""
    if not isinstance(stats, dict):
        return ""
    total_users = stats["totalUsers"] if "totalUsers" in stats else stats.get("users")
    total_servers = stats["totalServers"] if "totalServers" in stats else stats.get("servers")
    connected = stats["connectedClients"] if "connectedClients" in stats else stats.get("usersConnected")
    lines = []
    if total_users is not None:
        lines.append(f"• Total users: {int(total_users)}")
    if total_servers is not None:
        lines.append(f"• Total servers: {int(total_servers)}")
    if connected is not None:
        lines.append(f"• Connected via Psiphon Conduit: {int(connected)}")
    return "\n".join(lines)


# Tutorial Links
TUTORIALS = {
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

    # 1. Signup
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
        target = update.message.reply_text if update.message else update.callback_query.message.reply_text
        await target(err_text, parse_mode="Markdown")
        return ConversationHandler.END

    context.user_data["user_id"] = user_id
    context.user_data["telegram_id"] = str(user.id)

    stats = await api_request("GET", "/stats/aggregate")
    stats_block = _format_global_stats(stats)
    intro = (
        "👋 Welcome to Hope VPN Bot\n\n"
        "This bot helps you:\n"
        "• Create and manage VPN servers on supported providers\n"
        "• Check server health, verify VPN, and view metrics\n"
        "• Manage provider API tokens and SSH keys\n\n"
        "How it works\n"
        "1. Select a hosting provider from the options.\n"
        "2. Create an API token with that provider.\n"
        "3. Paste the token here and the bot provisions your server and adds it to Psiphon Conduit.\n\n"
        "Learn more\n"
        "• Psiphon Conduit: https://conduit.psiphon.ca/\n"
        "• Support: https://psiphon.ca/\n\n"
        "By continuing, you agree to our Terms of Service."
    )
    if stats_block:
        intro += "\n\n🌍 Network stats\n" + stats_block

    # 3. Check if user already has a selection setup
    has_existing = False
    try:
        selections = await api_request("GET", "/selections", params={"userId": str(user_id)})
        if isinstance(selections, list) and len(selections) > 0:
            has_existing = True
        # If user exists but has no selection, still show servers (if any)
        user_info = await api_request(
            "GET",
            "/user",
            params={"userId": str(user_id), "telegramId": str(user.id)},
        )
        if user_info:
            info_selections = user_info.get("selections") or []
            if info_selections:
                api_user_id = user_info.get("userId")
                if api_user_id and api_user_id != user_id:
                    context.user_data["user_id"] = api_user_id
                    user_id = api_user_id
                has_existing = True
            servers = await api_request("GET", "/servers", params={"userId": str(user_id)})
            if servers:
                has_existing = True
    except Exception as e:
        logger.error(f"Error fetching user info: {e}")
        # Continue to provider selection even if user fetch fails (assuming new user flow)
    if has_existing:
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🧰 Manage servers", callback_data=CB_MANAGE_SERVERS)],
                [InlineKeyboardButton("🔐 Manage tokens", callback_data=CB_MANAGE_TOKENS)],
                [InlineKeyboardButton("➕ Add provider", callback_data=CB_ADD_PROVIDER)],
                [InlineKeyboardButton("🧹 Remove Account", callback_data=CB_DELETE_ACCOUNT)],
            ]
        )
        target = update.message.reply_text if update.message else update.callback_query.message.reply_text
        await target(intro, reply_markup=keyboard)
        return MAIN_MENU
    target = update.message.reply_text if update.message else update.callback_query.message.reply_text
    await target(intro)

    # 4. Get Providers
    providers = await api_request("GET", "/providers")
    if not providers:
        target = update.message.reply_text if update.message else update.callback_query.message.reply_text
        await target(
            "⚠️ **Error**: Could not fetch providers.\n"
            "The API might be reachable but returning empty data.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # One provider per row
    keyboard = []
    for p in providers:
        if p.get("isActive", True):
            btn = InlineKeyboardButton(p["name"].title(), callback_data=f"{CB_PROVIDER}{p['name']}")
            keyboard.append([btn])

    target = update.message.reply_text if update.message else update.callback_query.message.reply_text
    await target(
        "Please select a provider to host your VPN server:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_PROVIDER


async def _show_token_prompt(query, provider_name: str, action: str) -> None:
    """Show token input prompt for a provider."""
    tutorial_link = TUTORIALS.get(provider_name.lower(), "https://google.com")
    title = "Update token" if action == "update" else "Add token"
    msg = (
        f"**{title}** for **{provider_name.title()}**.\n\n"
        "Please create an API token for your account and paste it in your next message.\n\n"
        f"Tutorial: {tutorial_link}\n\n"
        "_You can cancel and pick another provider with the button below, or send /cancel or /start to start over._"
    )
    cancel_btn = InlineKeyboardButton("◀️ Cancel – pick another provider", callback_data=CB_TOKEN_CANCEL)
    await query.edit_message_text(
        msg,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[cancel_btn]]),
    )


async def handle_provider_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save provider choice and ask for token."""
    query = update.callback_query
    await query.answer()

    provider_name = query.data.replace(CB_PROVIDER, "")
    context.user_data["provider"] = provider_name
    context.user_data["token_action"] = context.user_data.get("token_action") or "add"
    user_id = context.user_data.get("user_id")
    if user_id:
        selections = await api_request("GET", "/selections", params={"userId": str(user_id)}) or []
        for sel in selections:
            provider = (sel.get("Provider") or sel.get("provider")) or {}
            if (provider.get("name") or "").lower() == provider_name.lower():
                keyboard = [
                    [InlineKeyboardButton("🗑 Remove & add new token", callback_data=f"{CB_TOKEN_REPLACE}{provider_name}")],
                    [InlineKeyboardButton("🧰 Manage servers", callback_data=CB_MANAGE_SERVERS)],
                ]
                await query.edit_message_text(
                    f"✅ **{provider_name.title()} token already saved.**\n"
                    "Do you want to remove it and add a new one?\n\n"
                    "Tip: Use /manage to see your servers.",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown",
                )
                return SELECT_PROVIDER
    await _show_token_prompt(query, provider_name, context.user_data["token_action"])
    return ENTER_TOKEN


async def handle_existing_token_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle replace/keep when token already exists for provider."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = context.user_data.get("user_id")
    if data.startswith(CB_TOKEN_REPLACE):
        provider_name = data.replace(CB_TOKEN_REPLACE, "")
        if not user_id:
            await query.edit_message_text("⚠️ Session lost. Send /start to begin.")
            return ConversationHandler.END
        resp, err = await api_request_with_error(
            "DELETE", "/selections", json={"userId": user_id, "provider": provider_name}
        )
        if not resp:
            await query.edit_message_text(
                f"⚠️ Failed to remove token.\n\n_{err or 'Please try again.'}_",
                parse_mode="Markdown",
            )
            return SELECT_PROVIDER
        context.user_data["token_action"] = "add"
        await _show_token_prompt(query, provider_name, "add")
        return ENTER_TOKEN
    return SELECT_PROVIDER


async def handle_manage_servers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Open the server management list."""
    query = update.callback_query
    await query.answer()
    return await show_main_menu(update, context)


async def show_provider_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show provider selection list."""
    providers = await api_request("GET", "/providers")
    if not providers:
        target = update.callback_query.edit_message_text if update.callback_query else update.message.reply_text
        await target("Could not load providers. Try /start again.")
        return ConversationHandler.END
    keyboard = []
    for p in providers:
        if p.get("isActive", True):
            keyboard.append([InlineKeyboardButton(p["name"].title(), callback_data=f"{CB_PROVIDER}{p['name']}")])
    target = update.callback_query.edit_message_text if update.callback_query else update.message.reply_text
    await target(
        "Please select a provider to host your VPN server:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_PROVIDER


async def manage_servers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Command: /manage to show servers list."""
    if not context.user_data.get("user_id"):
        await update.message.reply_text("⚠️ Session lost. Send /start to begin.")
        return ConversationHandler.END
    return await show_main_menu(update, context)

async def handle_token_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User cancelled token step – show provider selection again."""
    query = update.callback_query
    await query.answer()
    if context.user_data.get("return_to") == "manage_tokens":
        return await show_manage_tokens(update, context)
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

    if context.user_data.get("token_action") == "update":
        await update.message.reply_text("✅ Token updated successfully!")
    else:
        await update.message.reply_text("✅ Token verified successfully!")
    context.user_data.pop("token_action", None)

    stats = await api_request("GET", "/stats/aggregate", params={"includeMetrics": "1"})
    provider_name = provider.title() if provider else "your provider"
    impact_text = _format_global_stats(stats) or "• Every contribution helps."

    await update.message.reply_text(
        "💡 **About your token**\n\n"
        f"We use your {provider_name} token only to create and manage servers.\n"
        "Estimated cost: ~5 EUR per month per server (billed by the provider).\n\n"
        "**Network stats**\n"
        f"{impact_text}\n\n"
        "Your contribution helps keep the internet free and open.",
        parse_mode="Markdown",
    )
    if context.user_data.get("return_to") == "manage_tokens":
        return await show_manage_tokens(update, context)
    return await show_main_menu(update, context)

def _build_server_list_content(servers: list, stats: Optional[dict]) -> str:
    """Build server list text (no keyboard)."""
    server_count = len(servers)
    text = f"Servers ({server_count})\n\n"

    if not servers:
        text += "No servers yet. Use the buttons below to create one."
        return text

    for i, s in enumerate(servers, 1):
        emoji, status_label = _vpn_ready_label(s)
        label = (s.get("label") or s.get("id", "")[:8]).strip() or f"Server {i}"
        text += f"{i}. {label} — {emoji} {status_label}\n"
        text += "\n"
    return text.strip()


def _build_server_list_keyboard(servers: list) -> Optional[InlineKeyboardMarkup]:
    """Build server list keyboard with a back button."""
    keyboard = []
    for i, s in enumerate(servers, 1):
        emoji, _ = _vpn_ready_label(s)
        label = (s.get("label") or s.get("id", "")[:8]).strip() or f"Server {i}"
        keyboard.append([InlineKeyboardButton(f"{emoji} {label}", callback_data=f"{CB_SERVER}{s['id']}")])
        keyboard.append([
            InlineKeyboardButton("ℹ️ Info", callback_data=f"{CB_MANAGE}{s['id']}"),
            InlineKeyboardButton("🔄", callback_data=f"{CB_REFRESH_SERVER}{s['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"{CB_DELETE}{s['id']}"),
        ])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=CB_BACK)])
    return InlineKeyboardMarkup(keyboard) if keyboard else InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=CB_BACK)]])


async def show_manage_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show provider token management screen."""
    is_callback = bool(update.callback_query)
    user_id = context.user_data.get("user_id")
    telegram_id = context.user_data.get("telegram_id")
    if not user_id:
        text = "⚠️ Session lost. Send /start to begin."
        target = update.callback_query.edit_message_text if is_callback else update.message.reply_text
        await target(text)
        return ConversationHandler.END

    providers = await api_request("GET", "/providers")
    selections = await api_request("GET", "/selections", params={"userId": str(user_id)}) or []
    selection_map = {}
    for sel in selections:
        provider = (sel.get("Provider") or sel.get("provider")) or {}
        name = (provider.get("name") or "").lower()
        if name:
            selection_map[name] = sel

    text = (
        "🔐 **Provider Tokens**\n\n"
        "You can store **one token per provider**.\n"
        "Use Update to replace a token, or Remove to delete it.\n\n"
    )

    keyboard = []
    if providers:
        for p in providers:
            if not p.get("isActive", True):
                continue
            name = p["name"]
            has_token = name.lower() in selection_map
            status = "✅ Saved" if has_token else "➕ Not set"
            text += f"• **{name.title()}** — {status}\n"
            if has_token:
                keyboard.append([
                    InlineKeyboardButton(f"🔄 Update {name.title()}", callback_data=f"{CB_TOKEN_UPDATE}{name}"),
                    InlineKeyboardButton("🗑 Remove", callback_data=f"{CB_TOKEN_REMOVE}{name}"),
                ])
            else:
                keyboard.append([
                    InlineKeyboardButton(f"➕ Add {name.title()}", callback_data=f"{CB_TOKEN_UPDATE}{name}"),
                ])
    else:
        text += "No providers available."

    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=CB_BACK_TOKENS)])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if is_callback:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    return MAIN_MENU


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display user's servers (first message) and action buttons (second message)."""
    is_callback = bool(update.callback_query)
    user_id = context.user_data.get("user_id")
    if not user_id:
        text = "⚠️ Session lost. Send /start to begin."
        target = update.callback_query.edit_message_text if is_callback else update.message.reply_text
        await target(text)
        return ConversationHandler.END

    servers = await api_request("GET", "/servers", params={"userId": str(user_id)})
    if servers is None:
        text = "⚠️ Error fetching servers. Check the API is running and try /start again."
        target = update.callback_query.edit_message_text if is_callback else update.message.reply_text
        await target(text)
        return MAIN_MENU

    stats = await api_request("GET", "/stats/aggregate")
    text = _build_server_list_content(servers, stats)

    server_markup = _build_server_list_keyboard(servers)

    # Actions keyboard (second message)
    actions_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Create Server", callback_data=CB_CREATE), InlineKeyboardButton("🔄 Refresh", callback_data=CB_REFRESH)],
        [InlineKeyboardButton("🔐 Manage Tokens", callback_data=CB_MANAGE_TOKENS)],
        [InlineKeyboardButton("🧹 Remove Account & Servers", callback_data=CB_DELETE_ACCOUNT)],
    ])

    chat_id = update.effective_chat.id
    try:
        if is_callback:
            msg_id = context.user_data.get("main_menu_message_id")
            if msg_id is not None and context.user_data.get("main_menu_chat_id") == chat_id:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id,
                    text=text, reply_markup=server_markup,
                )
            else:
                await update.callback_query.edit_message_text(
                    text, reply_markup=server_markup,
                )
                context.user_data["main_menu_message_id"] = update.callback_query.message.message_id
                context.user_data["main_menu_chat_id"] = chat_id
        else:
            sent = await update.message.reply_text(
                text, reply_markup=server_markup,
            )
            context.user_data["main_menu_message_id"] = sent.message_id
            context.user_data["main_menu_chat_id"] = chat_id
            await update.message.reply_text("Choose an action:", reply_markup=actions_markup)
    except BadRequest as e:
        if "not modified" not in (e.message or "").lower():
            raise
    return MAIN_MENU

async def handle_main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle clicks on Main Menu."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = context.user_data.get("user_id")

    if data == CB_REFRESH:
        return await show_main_menu(update, context)
        
    elif data == CB_MANAGE_SERVERS:
        return await show_main_menu(update, context)

    elif data == CB_BACK:
        return await start(update, context)

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

    elif data == CB_MANAGE_TOKENS:
        context.user_data["return_to"] = "manage_tokens"
        return await show_manage_tokens(update, context)

    elif data == CB_ADD_PROVIDER:
        context.user_data.pop("return_to", None)
        return await show_provider_selection(update, context)

    elif data == CB_BACK_TOKENS:
        context.user_data.pop("return_to", None)
        return await show_main_menu(update, context)

    elif data.startswith(CB_TOKEN_UPDATE):
        provider_name = data.replace(CB_TOKEN_UPDATE, "")
        context.user_data["provider"] = provider_name
        context.user_data["return_to"] = "manage_tokens"
        # Determine if we are updating or adding
        selections = await api_request("GET", "/selections", params={"userId": str(user_id)}) or []
        has_token = False
        for sel in selections:
            provider = (sel.get("Provider") or sel.get("provider")) or {}
            if (provider.get("name") or "").lower() == provider_name.lower():
                has_token = True
                break
        context.user_data["token_action"] = "update" if has_token else "add"
        await _show_token_prompt(query, provider_name, context.user_data["token_action"])
        return ENTER_TOKEN

    elif data.startswith(CB_TOKEN_REMOVE):
        provider_name = data.replace(CB_TOKEN_REMOVE, "")
        resp, err = await api_request_with_error(
            "DELETE", "/selections", json={"userId": user_id, "provider": provider_name}
        )
        if not resp:
            await query.message.reply_text(
                f"⚠️ Failed to remove token.\n\n_{err or 'Please try again.'}_",
                parse_mode="Markdown",
            )
        else:
            await query.message.reply_text("✅ Token removed.")
        return await show_manage_tokens(update, context)

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
        
    # Format details (vendor-neutral status)
    api_status = server.get("status", "unknown")
    emoji, status_label = _status_display(api_status)
    ip = server.get("ipAddress") or "—"
    vpn_status = server.get("vpnInstallStatus") or "—"
    vpn_msg = (server.get("vpnInstallMessage") or "").strip()
    vpn_line = f"VPN: {vpn_status}"
    if vpn_msg and vpn_msg != vpn_status:
        vpn_line += f" — {vpn_msg}"
    vpn_line += "\n"
    verify_hint = ""
    if vpn_status == "installed" and "verified" not in vpn_msg.lower():
        verify_hint = "\nUse Verify VPN below to confirm Conduit is running.\n"
    text = (
        "ℹ️ Server Details\n\n"
        f"ID: {server['id']}\n"
        f"IP: {ip}\n"
        f"SSH: ssh -i server-key.pem root@{ip}\n"
        f"Status: {emoji} {status_label}\n"
        f"{vpn_line}"
        f"{verify_hint}"
    )
    
    keyboard = [
        [InlineKeyboardButton("🇮🇷 Iran reachability", callback_data=f"{CB_CHECK}{server_id}")],
        [InlineKeyboardButton("🔍 Verify VPN", callback_data=f"{CB_VPN_VERIFY}{server_id}")],
        [InlineKeyboardButton("📊 Metrics", callback_data=f"{CB_METRICS}{server_id}")],
        [InlineKeyboardButton("🗑 Delete Server", callback_data=f"{CB_DELETE}{server_id}")],
        [InlineKeyboardButton("🔙 Back to List", callback_data=CB_BACK)]
    ]
    
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return SERVER_DETAILS

async def handle_server_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle actions in Server Details view."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = context.user_data.get("user_id")
    
    if data == CB_BACK:
        return await show_main_menu(update, context)
    elif data == CB_BACK_DETAILS:
        return await show_server_details(update, context)
        
    elif data.startswith(CB_DELETE):
        server_id = data.replace(CB_DELETE, "")
        await query.edit_message_text("🗑 Deleting server...")
        
        await api_request("DELETE", f"/servers/{server_id}", json={"userId": user_id})
        await query.edit_message_text("✅ Server deleted.")
        return await show_main_menu(update, context)

    elif data.startswith(CB_CHECK):
        server_id = data.replace(CB_CHECK, "")
        await query.edit_message_text("🇮🇷 Checking Iran reachability via check-host.net...")
        result = await api_request("GET", f"/servers/{server_id}/check", params={"userId": str(user_id)})
        if not result:
            await query.edit_message_text(
                "⚠️ Check-host request failed. Try again.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=CB_BACK_DETAILS)]]),
            )
            return SERVER_DETAILS
        health = result.get("health") or {}
        status = health.get("status") or ("reachable" if health.get("iranAccessible") else "unreachable")
        status_line = {
            "reachable": "✅ Reachable from Iran nodes",
            "unreachable": "⚠️ Not reachable from Iran nodes",
            "inconclusive": "⚠️ Inconclusive from Iran nodes",
            "rate_limited": "⚠️ Check-host rate limit exceeded",
        }.get(status, "⚠️ Check-host result unknown")
        msg = (health.get("message") or "").strip()
        text = "🇮🇷 Iran reachability (check-host.net)\n\n" + status_line
        if msg:
            text += f"\n\n{msg}"
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=CB_BACK_DETAILS)]]),
        )
        return SERVER_DETAILS

    elif data.startswith(CB_VPN_VERIFY):
        server_id = data.replace(CB_VPN_VERIFY, "")
        await query.edit_message_text("🔍 Verifying VPN on server (SSH check)...")
        result, err = await api_request_with_error(
            "GET", f"/servers/{server_id}/vpn-verify", params={"userId": str(user_id)}, timeout=15.0
        )
        if not result:
            await query.edit_message_text(
                f"⚠️ Verify failed.\n\n{err or 'Server unreachable.'}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=CB_BACK_DETAILS)]]),
            )
            return SERVER_DETAILS
        if result.get("ok"):
            msg = "✅ VPN verified\n\nConduit is running on port 9090. Status updated."
        else:
            msg = (
                "⚠️ VPN not running\n\n"
                "Conduit is not listening on port 9090. Status updated to reflect this.\n"
                "Install or start Conduit on the server to use metrics."
            )
        await query.edit_message_text(
            msg,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=CB_BACK_DETAILS)]]),
        )
        return SERVER_DETAILS

    elif data.startswith(CB_SSH_KEY):
        server_id = data.replace(CB_SSH_KEY, "")
        await query.edit_message_text("🔑 Fetching SSH key...")
        key_resp, err = await api_request_with_error(
            "GET", f"/servers/{server_id}/ssh-key", params={"userId": str(user_id)}, timeout=15.0
        )
        if not key_resp or not key_resp.get("privateKey"):
            await query.message.reply_text(
                f"⚠️ Could not fetch SSH key.\n\n_{err or 'Key not available.'}_",
                parse_mode="Markdown",
            )
            return await show_server_details(update, context)
        server = await api_request("GET", f"/servers/{server_id}", params={"userId": str(user_id)})
        ip = (server or {}).get("ipAddress") or "SERVER_IP"
        filename = f"server-{server_id[:8]}.pem"
        bio = BytesIO(key_resp["privateKey"].encode("utf-8"))
        bio.name = filename
        caption = (
            f"**SSH key file:** `{filename}`\n\n"
            "Run in Terminal:\n"
            f"`chmod 600 {filename}`\n"
            f"`ssh -i {filename} root@{ip}`\n\n"
            "_Keep this key private._"
        )
        await query.message.reply_document(document=bio, caption=caption, parse_mode="Markdown")
        return await show_server_details(update, context)

    elif data.startswith(CB_METRICS):
        server_id = data.replace(CB_METRICS, "")
        await query.edit_message_text("📊 Fetching metrics...")
        result, err = await api_request_with_error(
            "GET", f"/servers/{server_id}/metrics", params={"userId": str(user_id)}, timeout=15.0
        )
        if not result:
            reason = err or "Server unreachable or Conduit not running."
            if "9090" in reason and ("Connection refused" in reason or "refused" in reason.lower()):
                msg = (
                    "⚠️ Could not fetch metrics\n\n"
                    "Conduit is not running on this server, or it is not listening on port 9090.\n\n"
                    "Start Conduit (or install the VPN stack) on the server to see metrics here."
                )
            else:
                msg = f"⚠️ Could not fetch metrics.\n\n{reason}"
            await query.edit_message_text(
                msg,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=CB_BACK_DETAILS)]]),
            )
            return SERVER_DETAILS
        raw = result.get("metrics") or ""
        summary = _format_conduit_metrics(raw)
        text = "📊 Server metrics (Conduit)\n\n" + summary
        if len(text) > 4000:
            text = text[:3990] + "\n…"
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=CB_BACK_DETAILS)]]),
        )
        return SERVER_DETAILS

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
                CallbackQueryHandler(handle_provider_selection, pattern=f"^{CB_PROVIDER}"),
                CallbackQueryHandler(handle_existing_token_choice, pattern=f"^{CB_TOKEN_REPLACE}"),
                CallbackQueryHandler(handle_manage_servers_callback, pattern=f"^{CB_MANAGE_SERVERS}$"),
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
    application.add_handler(CommandHandler("manage", manage_servers))
    
    print(f"Bot is running... (API: {API_BASE_URL})")
    print("Tip: use 'python dev.py' for auto-restart on file changes.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
