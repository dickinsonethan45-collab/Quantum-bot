# Quantum Bot

`/redeem` posts a Discord embed with a **Redeem Key** button. The button opens
a private modal where the user enters a key. A valid key grants the configured
role. Both redeemed keys and users are stored in `redemptions.db`, so they
cannot redeem again after the bot restarts.

The bot also polls SellAuth for completed **Quantum Supporter** purchases. If
the buyer connected Discord during checkout, the delivered supporter key is
sent by DM in an orange Quantum-branded embed. Successful deliveries are saved
in `redemptions.db`, preventing duplicate DMs after restarts.

## Setup

1. Install Python 3.10 or newer.
2. Open a terminal in this folder and run:

   ```powershell
   py -m pip install -r requirements.txt
   ```

3. Set the `BOT_TOKEN`, `SELLAUTH_API_KEY`, and
   `SELLAUTH_ORDER_WEBHOOK_URL` environment variables. For local development,
   copy `.env.example` to `.env`; the bot loads it automatically. Never commit it.
4. Keep local supporter keys in `keys.txt`, or set `SUPPORTER_KEYS` to all keys
   separated by commas, spaces, or new lines.
5. In the Discord Developer Portal, invite the bot with the `bot` and
   `applications.commands` scopes. Give it **Manage Roles** permission.
6. In Server Settings > Roles, move the bot's role above the buyer role.
7. Start it:

   ```powershell
   py bot.py
   ```

If `GUILD_ID` is set, `/redeem` normally appears immediately in that server.
Global commands can take longer to appear.

## Railway deployment

1. Create a new GitHub repository and upload the files tracked by Git. The
   `.gitignore` excludes the live token, supporter keys, database, and backups.
2. In Railway, create a project from the GitHub repository.
3. In the service **Variables** tab, add:

   - `BOT_TOKEN`
   - `SELLAUTH_API_KEY`
   - `SELLAUTH_ORDER_WEBHOOK_URL`
   - `SUPPORTER_KEYS`
   - `DATA_DIR=/data`

4. Add a Railway Volume and mount it at `/data`. This preserves redeemed keys,
   delivered-DM records, webhook records, and saved panel message IDs when the
   service redeploys.
5. Deploy. `railway.json` starts the worker with `python bot.py`.

Do not run a local copy and the Railway copy simultaneously with the same bot
token. Discord will disconnect one of the competing sessions.

## Mod update command

Only the Discord application owner can run `/mod update`. It accepts optional
`added`, `fixed`, `changed`, and `removed` messages, then posts an orange
Quantum-branded ANSI changelog in the configured update channel.

Buyers must connect Discord during SellAuth checkout and allow DMs from server
members. If DMs are closed, the bot logs the failure and retries later.

Completed SellAuth payments are also posted once to the configured private
Discord order webhook. On the first run after enabling it, existing orders are
recorded without being reposted; later completed payments generate the order log.

## Automatic purchase panel

When the bot starts, it posts the orange **Purchase Quantum** store panel in the
configured purchase channel. The message ID is saved in `redemptions.db`, so
restarting the bot does not create duplicate panels. If the original panel is
deleted, the bot replaces it the next time it starts.

## Files

- `bot.py` - the bot and redemption logic
- `config.py` - public IDs and settings; secrets come from environment variables
- `.env.example` - safe Railway/local variable-name template
- `keys.txt` - local allowed keys; intentionally excluded from Git
- `purchase-banner.png` - full-width image used by the purchase panel
- `railway.json` - Railway start and restart configuration
- `redemptions.db` - runtime state; stored on the Railway Volume in production

If a bot token, SellAuth API key, or Discord webhook URL is ever exposed, rotate
it immediately and update the corresponding Railway variable.
