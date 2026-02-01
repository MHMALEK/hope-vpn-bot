#!/usr/bin/env python3
"""
Telegram bot that interacts with the Hope VPN API.
Simple structure: commands, callbacks, and a Back button that always goes to Main menu.
"""

import logging
import os
from io import BytesIO
from typing import Optional, Tuple

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
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

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:3000")

# Callback data: one "main" for back-to-main, rest are action + optional id
CB_MAIN = "main"
CB_MANAGE_SERVERS = "manage_servers"
CB_CREATE_SERVER = "create_server"
CB_REPLACE_TOKEN = "replace_token"
CB_DELETE_ACCOUNT = "delete_account"
CB_CONFIRM_DELETE = "confirm_delete"
CB_CANCEL_DELETE = "cancel_delete"
CB_TOKEN_CANCEL = "token_cancel"
CB_SERVER = "srv_"  # srv_<id> = open server
CB_CHECK = "check_"
CB_VPN_VERIFY = "vpn_"
CB_METRICS = "metrics_"

HETZNER_TOKEN_LINK = (
    "https://docs.hetzner.com/cloud/api/getting-started/generating-api-token/"
)
STATUS_READY = ("active", "running")
STATUS_ISSUE = ("error", "failed", "deleted")
STATUS_SETUP = ("creating", "provisioning", "booting", "pending", "starting")


def _status_display(api_status: str) -> Tuple[str, str]:
    s = (api_status or "").strip().lower()
    if s in STATUS_READY:
        return "✅", "Ready"
    if s in STATUS_ISSUE:
        return "❌", "Issue"
    if s in STATUS_SETUP:
        return "⏳", "Setting up"
    return "⚪", api_status or "Unknown"


def _vpn_ready_label(server: dict) -> Tuple[str, str]:
    vpn_status = (server.get("vpnInstallStatus") or "").strip().lower()
    vpn_msg = (server.get("vpnInstallMessage") or "").strip().lower()
    api_status = (server.get("status") or "").strip().lower()
    if vpn_status in ("installed", "ready") or "verified" in vpn_msg:
        return "✅", "Ready"
    if (
        vpn_status in ("installing", "pending", "booting", "creating", "provisioning")
        or api_status in STATUS_SETUP
    ):
        return "⏳", "Loading"
    if vpn_status in ("failed", "error", "not_running") or api_status in STATUS_ISSUE:
        return "❌", "Error"
    return "⏳", "Loading"


def _parse_prometheus_metrics(raw: str) -> dict:
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
    return f"{int(n)} {units[idx]}" if idx == 0 else f"{n:.2f} {units[idx]}"


def _format_uptime(seconds: float) -> str:
    if seconds is None:
        return "N/A"
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, s = divmod(s, 60)
    parts = [f"{days}d"] if days else []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _format_conduit_metrics(raw: str) -> str:
    metrics = _parse_prometheus_metrics(raw)
    if not metrics:
        return "No Conduit metrics (VPN may still be installing)."
    connected = metrics.get("conduit_connected_clients")
    up = metrics.get("conduit_bytes_uploaded")
    down = metrics.get("conduit_bytes_downloaded")
    uptime = metrics.get("conduit_uptime_seconds")
    return "\n".join(
        [
            f"Connected clients: {int(connected) if connected is not None else 'N/A'}",
            f"Uploaded: {_format_bytes(up)}",
            f"Downloaded: {_format_bytes(down)}",
            f"Uptime: {_format_uptime(uptime)}",
        ]
    )


def _format_global_stats(stats: Optional[dict]) -> str:
    if not isinstance(stats, dict):
        return ""
    total_users = stats.get("totalUsers") or stats.get("users")
    total_servers = stats.get("totalServers") or stats.get("servers")
    connected = stats.get("connectedClients") or stats.get("usersConnected")
    lines = []
    if total_users is not None:
        lines.append(f"• Total users: {int(total_users)}")
    if total_servers is not None:
        lines.append(f"• Total servers: {int(total_servers)}")
    if connected is not None:
        lines.append(f"• Connected via Psiphon Conduit: {int(connected)}")
    return "\n".join(lines)


async def api_request(method: str, endpoint: str, json=None, params=None, timeout=5.0):
    url = f"{API_BASE_URL.rstrip('/')}{endpoint}"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.request(
                method, url, json=json, params=params, timeout=timeout
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(
                "API %s %s -> %s: %s",
                method,
                url,
                e.response.status_code,
                e.response.text,
            )
            return None
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            logger.error("API %s %s failed: %s", method, url, e)
            return None
        except Exception as e:
            logger.error("API Request Failed %s %s: %s", method, url, e)
            return None


def _parse_error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
    except Exception:
        pass
    return response.text[:200] if response.text else f"HTTP {response.status_code}"


async def api_request_with_error(
    method: str, endpoint: str, json=None, params=None, timeout=30.0
):
    url = f"{API_BASE_URL.rstrip('/')}{endpoint}"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.request(
                method, url, json=json, params=params, timeout=timeout
            )
            response.raise_for_status()
            return response.json(), None
        except httpx.HTTPStatusError as e:
            logger.error(
                "API %s %s -> %s: %s",
                method,
                url,
                e.response.status_code,
                e.response.text,
            )
            return None, _parse_error_message(e.response)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            logger.error("API %s %s failed: %s", method, url, e)
            return None, "Could not reach the API. Check it is running."
        except Exception as e:
            logger.error("API Request Failed %s %s: %s", method, url, e)
            return None, str(e) or "Request failed"


async def _get_user_id(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Optional[str]:
    """Get user_id from context or restore from API by telegram ID."""
    user_id = context.user_data.get("user_id")
    if user_id:
        return user_id
    user = update.effective_user
    if not user:
        return None
    user_info = await api_request("GET", "/user", params={"telegramId": str(user.id)})
    if not user_info or not user_info.get("userId"):
        return None
    user_id = user_info["userId"]
    context.user_data["user_id"] = user_id
    context.user_data["telegram_id"] = str(user.id)
    return user_id


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Manage your servers", callback_data=CB_MANAGE_SERVERS
                )
            ],
            [InlineKeyboardButton("🔑 Replace token", callback_data=CB_REPLACE_TOKEN)],
            [
                InlineKeyboardButton(
                    "🧹 Remove account", callback_data=CB_DELETE_ACCOUNT
                )
            ],
        ]
    )


def _back_to_main_button() -> list:
    return [InlineKeyboardButton("🔙 Main menu", callback_data=CB_MAIN)]


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show main menu (welcome + Manage servers, Replace token, Remove account)."""
    user_id = await _get_user_id(update, context)
    if not user_id:
        text = "⚠️ Session lost. Send /start to begin."
        await _reply_or_edit(update, context, text)
        return

    stats = await api_request("GET", "/stats/aggregate")
    stats_block = _format_global_stats(stats)
    text = (
        "👋 **Hope VPN Bot**\n\n"
        "• View and manage your Conduit servers\n"
        "• Replace your Hetzner token\n"
        "• Remove your account\n\n"
        "[Psiphon Conduit](https://conduit.psiphon.ca/) · [Support](https://psiphon.ca/)"
    )
    if stats_block:
        text += "\n\n**Network**\n" + stats_block
    await _reply_or_edit(update, context, text, reply_markup=_main_menu_keyboard())


async def show_server_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show list of servers + Add server + Main menu."""
    user_id = await _get_user_id(update, context)
    if not user_id:
        text = "⚠️ Session lost. Send /start to begin."
        await _reply_or_edit(
            update,
            context,
            text,
            reply_markup=InlineKeyboardMarkup([_back_to_main_button()]),
        )
        return

    raw = await api_request("GET", "/servers", params={"userId": str(user_id)})
    if raw is None:
        text = "⚠️ Error fetching servers. Try again."
        await _reply_or_edit(
            update,
            context,
            text,
            reply_markup=InlineKeyboardMarkup([_back_to_main_button()]),
        )
        return

    if isinstance(raw, list):
        servers = raw
    elif isinstance(raw, dict):
        servers = raw.get("servers") or raw.get("data") or []
        if not isinstance(servers, list):
            servers = []
    else:
        servers = []

    text = "**Servers**\n\nTap a server for details."
    if not servers:
        text += "\n\nNo servers yet. Add one below."
    keyboard = []
    for i, s in enumerate(servers, 1):
        emoji, _ = _vpn_ready_label(s)
        label = (s.get("label") or (s.get("id") or "")[:8] or f"Server {i}").strip()
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{emoji} {label}", callback_data=f"{CB_SERVER}{s['id']}"
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("➕ Add new server", callback_data=CB_CREATE_SERVER)]
    )
    keyboard.append(_back_to_main_button())
    await _reply_or_edit(
        update, context, text, reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_server_details(
    update: Update, context: ContextTypes.DEFAULT_TYPE, server_id: str
) -> None:
    """Show one server's details + actions + Main menu."""
    user_id = context.user_data.get("user_id")
    if not user_id:
        await show_main_menu(update, context)
        return
    server = await api_request(
        "GET", f"/servers/{server_id}", params={"userId": user_id}
    )
    if not server:
        await _reply_or_edit(
            update,
            context,
            "Server not found.",
            reply_markup=InlineKeyboardMarkup([_back_to_main_button()]),
        )
        return
    api_status = server.get("status", "unknown")
    emoji, status_label = _status_display(api_status)
    ip = server.get("ipAddress") or "—"
    vpn_status = server.get("vpnInstallStatus") or "—"
    vpn_msg = (server.get("vpnInstallMessage") or "").strip()
    vpn_line = f"VPN: {vpn_status}"
    if vpn_msg and vpn_msg != vpn_status:
        vpn_line += f" — {vpn_msg}"
    text = f"ℹ️ **Server**\n\nIP: {ip}\nStatus: {emoji} {status_label}\n{vpn_line}"
    keyboard = [
        [
            InlineKeyboardButton(
                "🇮🇷 Iran reachability", callback_data=f"{CB_CHECK}{server_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🔍 Verify VPN Installation",
                callback_data=f"{CB_VPN_VERIFY}{server_id}",
            )
        ],
        [InlineKeyboardButton("📊 Metrics", callback_data=f"{CB_METRICS}{server_id}")],
        _back_to_main_button(),
    ]
    await _reply_or_edit(
        update, context, text, reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _reply_or_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None
) -> None:
    """Reply with message or edit current message if from callback."""
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=reply_markup, parse_mode="Markdown"
            )
        except Exception:
            await update.callback_query.message.reply_text(
                text, reply_markup=reply_markup, parse_mode="Markdown"
            )
    elif update.message:
        await update.message.reply_text(
            text, reply_markup=reply_markup, parse_mode="Markdown"
        )


# --- Commands ---


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry: signup, then show main menu or token prompt."""
    user = update.effective_user
    logger.info("User %s (%s) started.", user.first_name, user.id)
    signup_data = await api_request(
        "POST", "/signup", json={"telegramId": str(user.id)}
    )
    user_id = signup_data.get("userId") if signup_data else None
    if not user_id:
        text = (
            "👋 Welcome!\n\n"
            "⚠️ Could not connect to the API. Check it is running and try /start again."
        )
        await update.message.reply_text(text, parse_mode="Markdown")
        return
    context.user_data["user_id"] = user_id
    context.user_data["telegram_id"] = str(user.id)
    stats = await api_request("GET", "/stats/aggregate")
    stats_block = _format_global_stats(stats)
    user_info = await api_request(
        "GET", "/user", params={"userId": user_id, "telegramId": str(user.id)}
    )
    if user_info and user_info.get("userId") != user_id:
        context.user_data["user_id"] = user_info["userId"]
    selections = (user_info or {}).get("selections") or []
    servers_raw = await api_request(
        "GET", "/servers", params={"userId": str(context.user_data["user_id"])}
    )
    servers = servers_raw if isinstance(servers_raw, list) else []
    has_account = bool(selections or servers)
    if has_account:
        await show_main_menu(update, context)
        return
    intro = (
        "👋 **Welcome to Hope VPN Bot**\n\n"
        "**What we do**\n"
        "Hope VPN helps run **Psiphon Conduit** — a free, open tool that gives people in censored countries access to the open internet. We use your Hetzner Cloud account to create servers that run Conduit and donate bandwidth to the network.\n\n"
        "**What is Conduit?**\n"
        "Conduit is made by [Psiphon](https://psiphon.ca/) — the same team behind tools used by millions to bypass censorship. Conduit lets people in places where the internet is restricted (blocked sites, surveillance, throttling) connect to the global internet safely. Your server acts as a bridge: users connect through it to browse freely.\n\n"
        "**How you help the free internet**\n"
        "When you add a server, you donate a small slice of bandwidth. That bandwidth lets real people read news, talk to family, and access information that would otherwise be blocked. You’re not paying for their traffic — you’re just lending your server so the Conduit network can reach more people. Every server makes the network stronger and harder to shut down.\n\n"
        "**What you need to do**\n"
        "1. Create a Hetzner Cloud API token (link below).\n"
        "2. Paste the token here.\n"
        "3. We save it. Then go to **Manage servers** and add a server yourself — we create it and install Conduit.\n\n"
        f"**Create your token:** {HETZNER_TOKEN_LINK}\n\n"
        "[Conduit](https://conduit.psiphon.ca/) · [Psiphon / Support](https://psiphon.ca/)"
    )
    if stats_block:
        intro += "\n\n**Network**\n" + stats_block
    await update.message.reply_text(intro, parse_mode="Markdown")
    await update.message.reply_text(
        "Send your **Hetzner API token** in your next message (or /cancel):",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Cancel", callback_data=CB_TOKEN_CANCEL)]]
        ),
        parse_mode="Markdown",
    )
    context.user_data["awaiting_token"] = True


async def cmd_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show server list (with Main menu)."""
    await show_server_list(update, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting_token", None)
    await update.message.reply_text("Cancelled. Send /start to begin.")


# --- Message: token input ---


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text: if awaiting token, save token, create first server, then show main menu."""
    if not context.user_data.get("awaiting_token"):
        return
    token = update.message.text.strip()
    if not token:
        await update.message.reply_text(
            "Token cannot be empty. Send a valid token or /cancel."
        )
        return
    user_id = context.user_data.get("user_id")
    resp, err = await api_request_with_error(
        "POST",
        "/selections",
        json={"userId": user_id, "token": token, "provider": "hetzner"},
        timeout=15.0,
    )
    context.user_data.pop("awaiting_token", None)
    if not resp:
        await update.message.reply_text(
            f"❌ Failed to save token.\n\n_{err or 'Try again.'}_",
            parse_mode="Markdown",
        )
        return
    await update.message.reply_text(
        "✅ Token saved. Use **Manage servers** below to add a server.",
        parse_mode="Markdown",
    )
    await show_main_menu(update, context)


# --- Callback router: one handler for all buttons ---


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = context.user_data.get("user_id") or await _get_user_id(update, context)

    if data == CB_MAIN:
        await show_main_menu(update, context)
        return
    if data == CB_TOKEN_CANCEL:
        context.user_data.pop("awaiting_token", None)
        await show_main_menu(update, context)
        return
    if data == CB_MANAGE_SERVERS:
        await show_server_list(update, context)
        return
    if data == CB_CREATE_SERVER:
        if not user_id:
            await _reply_or_edit(
                update,
                context,
                "⚠️ Session lost. Send /start.",
                reply_markup=InlineKeyboardMarkup([_back_to_main_button()]),
            )
            return
        await query.edit_message_text(
            "🚀 Creating server… (30–90 sec)",
            reply_markup=InlineKeyboardMarkup([_back_to_main_button()]),
            parse_mode="Markdown",
        )
        resp, err = await api_request_with_error(
            "POST", "/servers/create", json={"userId": user_id}, timeout=60.0
        )
        if not resp:
            err_text = err or "Check token/funds."
            if "status code 403" in err_text or "status code 4" in err_text:
                err_text = (
                    "Your Hetzner account may have reached its server limit. "
                    "Delete a server in the Hetzner Cloud console or upgrade your account."
                )
            await query.edit_message_text(
                f"❌ Failed: _{err_text}_",
                reply_markup=InlineKeyboardMarkup([_back_to_main_button()]),
                parse_mode="Markdown",
            )
        else:
            await show_server_list(update, context)
        return
    if data == CB_REPLACE_TOKEN:
        context.user_data["awaiting_token"] = True
        context.user_data["return_to"] = "replace"
        await query.edit_message_text(
            "**Replace token**\n\nPaste your new Hetzner API token in your next message.\n\n"
            + HETZNER_TOKEN_LINK,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Cancel", callback_data=CB_TOKEN_CANCEL)]]
            ),
            parse_mode="Markdown",
        )
        return
    if data == CB_DELETE_ACCOUNT:
        await query.edit_message_text(
            "⚠️ **Delete account?**\n\nThis removes your account and saved token. Hetzner servers are not deleted.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Yes, delete", callback_data=CB_CONFIRM_DELETE
                        )
                    ],
                    _back_to_main_button(),
                ]
            ),
            parse_mode="Markdown",
        )
        return
    if data == CB_CANCEL_DELETE:
        await show_main_menu(update, context)
        return
    if data == CB_CONFIRM_DELETE:
        await query.edit_message_text("Deleting…")
        resp, err = await api_request_with_error(
            "DELETE", "/user", json={"userId": user_id}, timeout=20.0
        )
        context.user_data.clear()
        if resp:
            await query.message.reply_text(
                "✅ Account deleted. Send /start to begin again."
            )
        else:
            await query.message.reply_text(f"❌ Failed: {err or 'Try again.'}")
        return

    if data.startswith(CB_SERVER):
        server_id = data[len(CB_SERVER) :]
        context.user_data["selected_server_id"] = server_id
        await show_server_details(update, context, server_id)
        return

    if data.startswith(CB_CHECK):
        server_id = data[len(CB_CHECK) :]
        await query.edit_message_text("🇮🇷 Checking Iran reachability…")
        result = await api_request(
            "GET", f"/servers/{server_id}/check", params={"userId": str(user_id)}
        )
        if not result:
            text = "⚠️ Check failed."
        else:
            health = result.get("health") or {}
            status = health.get("status") or (
                "reachable" if health.get("iranAccessible") else "unreachable"
            )
            text = {
                "reachable": "✅ Reachable from Iran",
                "unreachable": "⚠️ Not reachable",
                "inconclusive": "⚠️ Inconclusive",
                "rate_limited": "⚠️ Rate limited",
            }.get(status, "⚠️ Unknown")
            if health.get("message"):
                text += "\n\n" + health.get("message", "")
        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup([_back_to_main_button()])
        )
        return
    if data.startswith(CB_VPN_VERIFY):
        server_id = data[len(CB_VPN_VERIFY) :]
        await query.edit_message_text("🔍 Verifying VPN…")
        result, err = await api_request_with_error(
            "GET",
            f"/servers/{server_id}/vpn-verify",
            params={"userId": str(user_id)},
            timeout=15.0,
        )
        if not result:
            text = f"⚠️ Verify failed: {err or 'Unreachable.'}"
        elif result.get("ok"):
            text = "✅ VPN verified. Conduit is running on port 9090."
        else:
            text = "⚠️ VPN not running. Conduit is not listening on port 9090."
        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup([_back_to_main_button()])
        )
        return
    if data.startswith(CB_METRICS):
        server_id = data[len(CB_METRICS) :]
        await query.edit_message_text("📊 Fetching metrics…")
        result, err = await api_request_with_error(
            "GET",
            f"/servers/{server_id}/metrics",
            params={"userId": str(user_id)},
            timeout=15.0,
        )
        if not result:
            text = err or "Could not fetch metrics."
        else:
            raw = result.get("metrics") or ""
            text = "📊 **Conduit metrics**\n\n" + _format_conduit_metrics(raw)
        if len(text) > 4000:
            text = text[:3990] + "\n…"
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([_back_to_main_button()]),
            parse_mode="Markdown",
        )
        return


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Warning: TELEGRAM_BOT_TOKEN not set.")
        return
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("manage", cmd_manage))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    print(f"Bot running (API: {API_BASE_URL})")
    if "localhost" in API_BASE_URL or "127.0.0.1" in API_BASE_URL:
        print("Warning: Set API_BASE_URL to your production API URL.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
