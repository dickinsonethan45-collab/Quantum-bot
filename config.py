"""Quantum Bot settings safe to commit to GitHub.

Secrets are loaded from Railway environment variables and are never stored in
this file.
"""

import os

from dotenv import load_dotenv


load_dotenv()


# Railway variables (required).
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SELLAUTH_API_KEY = os.getenv("SELLAUTH_API_KEY", "").strip()
SELLAUTH_ORDER_WEBHOOK_URL = os.getenv("SELLAUTH_ORDER_WEBHOOK_URL", "").strip()

# Discord server, role, and channel IDs.
BUYER_ROLE_ID = 1543046349757354167
GUILD_ID = 1542658696515944568
PANEL_CHANNEL_ID = 1543410599348801708
REDEMPTION_LOG_CHANNEL_ID = 1543661084659941397
MOD_UPDATE_CHANNEL_ID = 1542665123125006457
PURCHASE_CHANNEL_ID = 1543694414801666200
SUPPORT_PANEL_CHANNEL_ID = 1543010726937886790
KEY_STATUS_CHANNEL_ID = 1546379006423597126
KEY_STATUS_POLL_SECONDS = 30  # how often the bot re-checks for newly redeemed keys

# Public links.
QUANTUM_STORE_URL = "https://quantum1.mysellauth.com/"
SUPPORT_TICKET_URL = (
    "https://discord.com/channels/1542658696515944568/1543705338325372989"
)

# SellAuth polling settings.
SELLAUTH_SHOP_ID = 264838
SELLAUTH_PRODUCT_ID = 859964
SELLAUTH_POLL_SECONDS = 30

# Text shown by /redeem.
EMBED_TITLE = "Redeem Your Supporter Key"
EMBED_DESCRIPTION = (
    "Click the button below and enter your **Supporter key** to claim the supporter role."
)
BUTTON_LABEL = "Redeem Supporter Key"
