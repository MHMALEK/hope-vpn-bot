# Psiphon Conduit Telegram Bot

A Telegram bot that asks users to choose **Hertz** or **Digital Ocean** when they start, then collects and stores their API token for use in your code.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Clone the repo and install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and set your bot token:

   ```bash
   cp .env.example .env
   # Edit .env and set TELEGRAM_BOT_TOKEN=...
   ```

4. Run the bot:

   ```bash
   python bot.py
   ```

   **Development (auto re-run on file changes):**

   ```bash
   python dev.py
   ```

   This watches `bot.py` and restarts the bot when you save changes. Press Ctrl+C to stop.

## Flow

- User sends **/start** → sees inline buttons: **Hertz** | **Digital Ocean**.
- User taps a button → bot asks for the API token.
- User sends the token → bot stores it and confirms.
- User can send **/start** again to change provider or update the token.

## Using the token in your code

Tokens are kept in memory in `user_tokens` (keyed by Telegram `user_id`). To get the current user’s token:

```python
from bot import get_user_token, user_tokens

# By user_id (e.g. from update.effective_user.id)
data = get_user_token(user_id=123456789)
if data:
    provider = data["provider"]       # "provider_hertz"
    provider_name = data["provider_name"]  # "Hertz" 
    token = data["token"]             # The API token
    # Use token for Hertz or Digital Ocean API calls
```

- **Hertz**: use `token` with the Hertz API (e.g. in `Authorization` header).
- **Digital Ocean**: use `token` as the DO API token (e.g. `Bearer <token>`).

## Commands

- **/start** – Show provider options and start token flow.
- **/cancel** – Cancel the current flow (e.g. while waiting for token).
