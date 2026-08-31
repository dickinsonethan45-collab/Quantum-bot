from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config


BASE_DIR = Path(__file__).resolve().parent
KEYS_FILE = BASE_DIR / "keys.txt"
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR))).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_FILE = DATA_DIR / "redemptions.db"
LOGO_FILE = BASE_DIR / "logo.png"
PURCHASE_BANNER_FILE = BASE_DIR / "purchase-banner.png"
SUPPORTER_KEY_PATTERN = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$")
SUPPORTER_KEY_DM_ROLE_ID = 1542658696713080849
SUPPORTER_KEY_DM_INPUT_PATTERN = re.compile(
    r"^[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}$"
)
CREDENTIAL_ID_DM_INPUT_PATTERN = re.compile(r"^[A-Za-z0-9]{16}$")
QUANTUM_ROBUX_STORE_URL = "https://www.roblox.com/game-pass/1941834921/Quantum-Supporter"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("quantum-bot")


def normalize_key(value: str) -> str:
    return value.strip().upper()


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_FILE, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> int:
    key_lines: list[str] = []
    if KEYS_FILE.exists():
        key_lines.extend(KEYS_FILE.read_text(encoding="utf-8").splitlines())
    railway_keys = os.getenv("SUPPORTER_KEYS", "")
    if railway_keys:
        key_lines.extend(re.split(r"[\s,]+", railway_keys))

    keys = {
        normalize_key(line)
        for line in key_lines
        if line.strip() and not line.lstrip().startswith("#")
    }
    if not keys:
        raise RuntimeError(
            "No supporter keys configured. Add keys.txt locally or set the "
            "SUPPORTER_KEYS Railway variable."
        )

    database = connect_database()
    try:
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS redeem_keys (
                key TEXT PRIMARY KEY,
                redeemed_by INTEGER,
                redeemed_at TEXT
            )
            """
        )
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS redeemed_users (
                user_id INTEGER PRIMARY KEY,
                key TEXT NOT NULL UNIQUE,
                redeemed_at TEXT NOT NULL,
                FOREIGN KEY (key) REFERENCES redeem_keys(key)
            )
            """
        )
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS sellauth_dm_deliveries (
                invoice_id TEXT NOT NULL,
                discord_user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (invoice_id, key)
            )
            """
        )
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            )
            """
        )
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS sellauth_webhook_deliveries (
                invoice_id TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            )
            """
        )
        database.execute(
            """
            INSERT OR IGNORE INTO sellauth_webhook_deliveries (invoice_id, sent_at)
            SELECT invoice_id, MIN(sent_at)
            FROM sellauth_dm_deliveries
            GROUP BY invoice_id
            """
        )
        database.executemany(
            "INSERT OR IGNORE INTO redeem_keys (key) VALUES (?)",
            ((key,) for key in keys),
        )
        database.commit()
    finally:
        database.close()

    return len(keys)


def reserve_key(key: str, user_id: int) -> tuple[str, str | None]:
    """Atomically reserve a key for one user.

    Returns (status, detail), where status is success, invalid, used, or
    user_used. detail contains the existing key/user ID when relevant.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    database = connect_database()
    try:
        database.execute("BEGIN IMMEDIATE")

        existing_user = database.execute(
            "SELECT key FROM redeemed_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if existing_user:
            database.rollback()
            return "user_used", str(existing_user["key"])

        key_row = database.execute(
            "SELECT redeemed_by FROM redeem_keys WHERE key = ?", (key,)
        ).fetchone()
        if key_row is None:
            database.rollback()
            return "invalid", None
        if key_row["redeemed_by"] is not None:
            database.rollback()
            return "used", str(key_row["redeemed_by"])

        updated = database.execute(
            """
            UPDATE redeem_keys
            SET redeemed_by = ?, redeemed_at = ?
            WHERE key = ? AND redeemed_by IS NULL
            """,
            (user_id, timestamp, key),
        )
        if updated.rowcount != 1:
            database.rollback()
            return "used", None

        database.execute(
            """
            INSERT INTO redeemed_users (user_id, key, redeemed_at)
            VALUES (?, ?, ?)
            """,
            (user_id, key, timestamp),
        )
        database.commit()
        return "success", None
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def release_key(key: str, user_id: int) -> None:
    """Undo a reservation if Discord could not add the configured role."""
    database = connect_database()
    try:
        database.execute("BEGIN IMMEDIATE")
        database.execute(
            "DELETE FROM redeemed_users WHERE user_id = ? AND key = ?",
            (user_id, key),
        )
        database.execute(
            """
            UPDATE redeem_keys
            SET redeemed_by = NULL, redeemed_at = NULL
            WHERE key = ? AND redeemed_by = ?
            """,
            (key, user_id),
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def sellauth_dm_was_sent(invoice_id: str, key: str) -> bool:
    database = connect_database()
    try:
        row = database.execute(
            """
            SELECT 1 FROM sellauth_dm_deliveries
            WHERE invoice_id = ? AND key = ?
            """,
            (invoice_id, key),
        ).fetchone()
        return row is not None
    finally:
        database.close()


def mark_sellauth_dm_sent(invoice_id: str, user_id: int, key: str) -> None:
    database = connect_database()
    try:
        database.execute(
            """
            INSERT OR IGNORE INTO sellauth_dm_deliveries
                (invoice_id, discord_user_id, key, sent_at)
            VALUES (?, ?, ?, ?)
            """,
            (invoice_id, user_id, key, datetime.now(timezone.utc).isoformat()),
        )
        database.commit()
    finally:
        database.close()


def sellauth_webhook_was_sent(invoice_id: str) -> bool:
    database = connect_database()
    try:
        row = database.execute(
            "SELECT 1 FROM sellauth_webhook_deliveries WHERE invoice_id = ?",
            (invoice_id,),
        ).fetchone()
        return row is not None
    finally:
        database.close()


def mark_sellauth_webhook_sent(invoice_id: str) -> None:
    database = connect_database()
    try:
        database.execute(
            """
            INSERT OR IGNORE INTO sellauth_webhook_deliveries (invoice_id, sent_at)
            VALUES (?, ?)
            """,
            (invoice_id, datetime.now(timezone.utc).isoformat()),
        )
        database.commit()
    finally:
        database.close()


def get_bot_setting(key: str) -> str | None:
    database = connect_database()
    try:
        row = database.execute(
            "SELECT setting_value FROM bot_settings WHERE setting_key = ?", (key,)
        ).fetchone()
        return str(row["setting_value"]) if row else None
    finally:
        database.close()


def set_bot_setting(key: str, value: str) -> None:
    database = connect_database()
    try:
        database.execute(
            """
            INSERT INTO bot_settings (setting_key, setting_value)
            VALUES (?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value
            """,
            (key, value),
        )
        database.commit()
    finally:
        database.close()


def extract_invoice_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    for field in ("data", "invoices", "results"):
        value = payload.get(field)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = value.get("data")
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


def invoice_is_completed(invoice: dict[str, Any]) -> bool:
    status = str(invoice.get("status") or "").lower()
    if status in {
        "cancelled",
        "canceled",
        "expired",
        "failed",
        "refunded",
        "chargeback",
        "partially_completed",
    }:
        return False
    return bool(invoice.get("completed_at")) or status in {
        "completed",
        "paid",
    }


def extract_discord_user_id(invoice: dict[str, Any]) -> int | None:
    for field in ("discord_user_id", "discord_id", "discordId"):
        value = invoice.get(field)
        if value is not None and str(value).isdigit():
            return int(value)

    discord_user = invoice.get("discord_user")
    if isinstance(discord_user, dict):
        value = discord_user.get("id")
        if value is not None and str(value).isdigit():
            return int(value)

    custom_fields = invoice.get("custom_fields")
    if isinstance(custom_fields, dict):
        for field in ("discord_user_id", "discord_id"):
            value = custom_fields.get(field)
            if value is not None and str(value).isdigit():
                return int(value)
    return None


def flatten_deliverables(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            decoded = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return [line.strip() for line in stripped.splitlines() if line.strip()]
        return flatten_deliverables(decoded)
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(flatten_deliverables(item))
        return result
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(flatten_deliverables(item))
        return result
    return [str(value).strip()]


def extract_supporter_keys(invoice: dict[str, Any]) -> list[str]:
    product_id = int(getattr(config, "SELLAUTH_PRODUCT_ID", 0) or 0)
    candidates: list[str] = []
    matched_item = False

    items = invoice.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            item_product_id = item.get("product_id")
            product = item.get("product")
            if item_product_id is None and isinstance(product, dict):
                item_product_id = product.get("id")
            if product_id and str(item_product_id) != str(product_id):
                continue
            matched_item = True
            candidates.extend(flatten_deliverables(item.get("delivered")))

    invoice_product_id = invoice.get("product_id")
    product = invoice.get("product")
    if invoice_product_id is None and isinstance(product, dict):
        invoice_product_id = product.get("id")
    if not product_id or str(invoice_product_id) == str(product_id) or matched_item:
        candidates.extend(flatten_deliverables(invoice.get("delivered")))

    keys: list[str] = []
    for candidate in candidates:
        key = normalize_key(candidate)
        if SUPPORTER_KEY_PATTERN.fullmatch(key) and key not in keys:
            keys.append(key)
    return keys


def first_invoice_value(invoice: dict[str, Any], *fields: str) -> Any:
    for field in fields:
        value = invoice.get(field)
        if value not in (None, "", []):
            return value
    return None


def order_customer_email(invoice: dict[str, Any]) -> str:
    value = first_invoice_value(invoice, "email", "customer_email", "buyer_email")
    customer = invoice.get("customer")
    if value is None and isinstance(customer, dict):
        value = first_invoice_value(customer, "email", "customer_email")
    return str(value or "Not provided")


def order_gateway(invoice: dict[str, Any]) -> str:
    value = first_invoice_value(
        invoice, "gateway", "gateway_name", "payment_method", "payment_gateway"
    )
    if isinstance(value, dict):
        value = first_invoice_value(value, "name", "title", "type")
    return str(value or "Unknown").upper()


def order_money(invoice: dict[str, Any], *fields: str) -> str:
    value = first_invoice_value(invoice, *fields)
    if value is None:
        return "Unknown"
    currency = str(first_invoice_value(invoice, "currency", "currency_code") or "USD")
    try:
        amount = f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)
    if currency.upper() == "USD":
        return f"${amount}"
    return f"{amount} {currency.upper()}"


def order_items_text(invoice: dict[str, Any]) -> str:
    rendered: list[str] = []
    items = invoice.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            product = item.get("product")
            product_name = first_invoice_value(item, "name", "product_name", "title")
            if product_name is None and isinstance(product, dict):
                product_name = first_invoice_value(product, "name", "title")
            quantity = first_invoice_value(item, "quantity", "qty") or 1
            item_text = f"{product_name or 'Quantum Supporter'} x{quantity}"
            rendered.append(item_text)

    if not rendered:
        product = invoice.get("product")
        product_name = first_invoice_value(invoice, "product_name", "item_name")
        if product_name is None and isinstance(product, dict):
            product_name = first_invoice_value(product, "name", "title")
        rendered.append(str(product_name or "Quantum Supporter"))
    return " | ".join(rendered)


def safe_order_log_value(value: Any, limit: int = 900) -> str:
    return str(value).replace("`", "'").replace("\r", " ").replace("\n", " ")[:limit]


async def send_sellauth_order_webhook(
    session: aiohttp.ClientSession, invoice: dict[str, Any], invoice_id: str
) -> None:
    webhook_url = str(getattr(config, "SELLAUTH_ORDER_WEBHOOK_URL", "")).strip()
    if not webhook_url:
        return

    email = safe_order_log_value(order_customer_email(invoice))
    coupon_value = first_invoice_value(
        invoice, "coupon", "coupon_code", "discount_code"
    )
    if isinstance(coupon_value, dict):
        coupon_value = first_invoice_value(coupon_value, "code", "name", "id")
    discount = safe_order_log_value(coupon_value or "none")
    charged = safe_order_log_value(
        order_money(invoice, "paid", "total", "total_amount", "amount", "price")
    )
    items = safe_order_log_value(order_items_text(invoice))
    supporter_keys = extract_supporter_keys(invoice)
    supporter_key_text = safe_order_log_value(
        ", ".join(supporter_keys) if supporter_keys else "Not provided"
    )
    order_number = safe_order_log_value(
        first_invoice_value(invoice, "id") or invoice_id, 120
    )
    details = (
        f"Email    : {email}\n"
        f"Discount : {discount}\n"
        f"Charged  : {charged}\n"
        f"Items    : {items}\n"
        f"Supporter Key : {supporter_key_text}\n"
        "Status   : completed"
    )
    payload = {
        "username": "Quantum Orders",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": f"🛒 Quantum order completed — #{order_number}",
                "description": f"```text\n{details}\n```",
                "color": 0xFF9100,
            }
        ],
    }
    async with session.post(webhook_url, json=payload) as response:
        response.raise_for_status()


async def send_purchase_key_dm(user: discord.User, key: str) -> None:
    embed = discord.Embed(
        title="Purchase Confirmed — Welcome to Quantum",
        description=(
            "Thank you for purchasing **Quantum**. Here is your supporter key — "
            "**DO NOT SHARE**."
        ),
        color=discord.Color.from_rgb(255, 145, 0),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Your Supporter Key", value=f"`{key}`", inline=False)
    embed.add_field(
        name="Quantum Message",
        value=(
            "To redeem your key, go to the Quantum Discord server and visit the "
            "[supporter key redemption channel]"
            "(https://discord.com/channels/1542658696515944568/1543410599348801708), "
            "then redeem your supporter key to receive the Supporter role."
        ),
        inline=False,
    )
    embed.set_footer(text="Quantum • Purchase delivery")

    if LOGO_FILE.exists():
        logo = discord.File(LOGO_FILE, filename="quantum-purchase-logo.png")
        embed.set_thumbnail(url="attachment://quantum-purchase-logo.png")
        await user.send(embed=embed, file=logo)
    else:
        logger.warning("Logo file is missing: %s", LOGO_FILE)
        await user.send(embed=embed)


async def send_supporter_key_confirmation_dm(
    user: discord.abc.User,
    key: str,
    paid_via: str,
    granted_by: discord.abc.User,
    credential_id: str | None = None,
) -> None:
    description = (
        "Thank you for your purchase, this is your Supporter key, you will "
        "need it to redeem the supporter role and to access Quantum Mods."
    )
    if credential_id:
        description = (
            "Your Supporter Key is used to redeem your Supporter role in our "
            "Discord server and access Quantum Mods in-game, while your "
            "Credential ID is used to log in to your Auth account. Please "
            "keep both your Supporter Key and Credential ID private and "
            "secure."
        )

    embed = discord.Embed(
        title="Quantum Purchase Confirmed",
        description=description,
        color=discord.Color.from_rgb(255, 145, 0),
    )
    embed.add_field(
        name="Your Supporter Key",
        value=f"```{key}```",
        inline=bool(credential_id),
    )
    if credential_id:
        embed.add_field(
            name="Your Credential ID",
            value=f"```{credential_id}```",
            inline=True,
        )
    embed.add_field(name="Paid Via", value=paid_via, inline=True)
    embed.add_field(name="Granted by", value=granted_by.mention, inline=True)

    if LOGO_FILE.exists():
        image_name = "quantum-supporter-logo.png"
        embed.set_thumbnail(url=f"attachment://{image_name}")
        await user.send(embed=embed, file=discord.File(LOGO_FILE, filename=image_name))
    else:
        logger.warning("Logo file is missing: %s", LOGO_FILE)
        await user.send(embed=embed)


async def fetch_sellauth_json(
    session: aiohttp.ClientSession, endpoint: str
) -> Any:
    url = f"https://api.sellauth.com/v1/{endpoint.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {config.SELLAUTH_API_KEY.strip()}",
        "Accept": "application/json",
        "User-Agent": "QuantumBot/1.0",
    }
    async with session.get(url, headers=headers) as response:
        response.raise_for_status()
        return await response.json(content_type=None)


async def send_redemption_log(member: discord.Member, key: str) -> None:
    """Post a successful redemption to the configured private log channel."""
    if config.REDEMPTION_LOG_CHANNEL_ID <= 0:
        return

    channel = bot.get_channel(config.REDEMPTION_LOG_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(config.REDEMPTION_LOG_CHANNEL_ID)
    if not isinstance(channel, discord.abc.Messageable):
        raise TypeError("Configured redemption log channel cannot receive messages")

    embed = discord.Embed(
        title="Quantum Buyer role granted",
        description="A buyer has successfully redeemed a supporter key.",
        color=discord.Color.from_rgb(255, 145, 0),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Buyer",
        value=f"{member.mention}\n`{member.id}`",
        inline=True,
    )
    embed.add_field(name="Key", value=f"`{key}`", inline=True)
    embed.set_footer(text="Quantum Bot • Redemption log")

    if LOGO_FILE.exists():
        logo = discord.File(LOGO_FILE, filename="quantum-log-logo.png")
        embed.set_thumbnail(url="attachment://quantum-log-logo.png")
        await channel.send(embed=embed, file=logo)
    else:
        logger.warning("Logo file is missing: %s", LOGO_FILE)
        await channel.send(embed=embed)


class RedeemModal(discord.ui.Modal, title="Redeem Your Key"):
    key_input = discord.ui.TextInput(
        label="Key",
        placeholder="0000-0000-0000",
        required=True,
        min_length=14,
        max_length=32,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send(
                "Keys can only be redeemed inside the Discord server.", ephemeral=True
            )
            return

        if config.BUYER_ROLE_ID <= 0:
            await interaction.followup.send(
                "The bot owner has not configured the buyer role yet.", ephemeral=True
            )
            return

        role = interaction.guild.get_role(config.BUYER_ROLE_ID)
        if role is None:
            await interaction.followup.send(
                "The configured buyer role could not be found. Please contact an admin.",
                ephemeral=True,
            )
            return

        key = normalize_key(str(self.key_input.value))
        status, _ = await asyncio.to_thread(reserve_key, key, interaction.user.id)

        if status == "invalid":
            await interaction.followup.send("That key is invalid.", ephemeral=True)
            return
        if status == "used":
            await interaction.followup.send(
                "That key has already been redeemed.", ephemeral=True
            )
            return
        if status == "user_used":
            if role not in interaction.user.roles:
                try:
                    await interaction.user.add_roles(
                        role, reason="Restoring role for an existing key redemption"
                    )
                except discord.HTTPException:
                    logger.exception("Could not restore role for user %s", interaction.user.id)
            await interaction.followup.send(
                "You have already redeemed a key.", ephemeral=True
            )
            return

        try:
            await interaction.user.add_roles(
                role, reason=f"Redeemed Quantum Bot key: {key}"
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not add role to user %s", interaction.user.id)
            await asyncio.to_thread(release_key, key, interaction.user.id)
            await interaction.followup.send(
                "I could not add the role. Make sure my bot role is above the buyer "
                "role and I have **Manage Roles**, then try again.",
                ephemeral=True,
            )
            return

        try:
            await send_redemption_log(interaction.user, key)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException, TypeError):
            logger.exception(
                "Could not send redemption log for user %s", interaction.user.id
            )

        await interaction.followup.send(
            f"Key redeemed successfully — you now have the {role.mention} role!",
            ephemeral=True,
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        logger.exception("Redeem modal failed", exc_info=error)
        message = "Something went wrong while redeeming that key. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class RedeemView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label=config.BUTTON_LABEL,
        style=discord.ButtonStyle.success,
        custom_id="quantum_bot:redeem_key",
    )
    async def redeem_button(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(RedeemModal())


class QuantumBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self._purchase_panel_checked = False
        self._support_ticket_panel_checked = False

    async def setup_hook(self) -> None:
        loaded_keys = await asyncio.to_thread(initialize_database)
        logger.info("Loaded %s unique redemption keys", loaded_keys)
        self.add_view(RedeemView())

        sellauth_key = str(getattr(config, "SELLAUTH_API_KEY", "")).strip()
        if sellauth_key and sellauth_key != "PASTE_YOUR_SELLAUTH_API_KEY_HERE":
            interval = max(15, int(getattr(config, "SELLAUTH_POLL_SECONDS", 30)))
            self.sellauth_purchase_poll.change_interval(seconds=interval)
            self.sellauth_purchase_poll.start()
            logger.info("SellAuth purchase delivery polling enabled (%ss)", interval)
        else:
            logger.warning(
                "SellAuth purchase DMs are disabled until SELLAUTH_API_KEY is set"
            )

        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %s command(s) to guild %s", len(synced), config.GUILD_ID)
        else:
            synced = await self.tree.sync()
            logger.info("Synced %s global command(s)", len(synced))

    async def process_sellauth_purchases(self) -> None:
        shop_id = int(getattr(config, "SELLAUTH_SHOP_ID", 0) or 0)
        if shop_id <= 0:
            logger.error("SELLAUTH_SHOP_ID is not configured")
            return

        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            payload = await fetch_sellauth_json(
                session, f"shops/{shop_id}/invoices"
            )
            invoices = extract_invoice_list(payload)
            webhook_url = str(
                getattr(config, "SELLAUTH_ORDER_WEBHOOK_URL", "")
            ).strip()
            webhook_baseline_key = "sellauth_order_webhook_initialized"
            webhook_initialized = await asyncio.to_thread(
                get_bot_setting, webhook_baseline_key
            )
            initialize_webhook_baseline = bool(webhook_url) and webhook_initialized != "1"

            for summary in reversed(invoices):
                if not invoice_is_completed(summary):
                    continue

                detail_id = summary.get("id")
                if detail_id is None:
                    continue

                detail_payload = await fetch_sellauth_json(
                    session, f"shops/{shop_id}/invoices/{detail_id}"
                )
                if not isinstance(detail_payload, dict):
                    continue
                invoice = detail_payload.get("data", detail_payload)
                if not isinstance(invoice, dict) or not invoice_is_completed(invoice):
                    continue

                invoice_id = str(
                    invoice.get("unique_id")
                    or invoice.get("id")
                    or summary.get("unique_id")
                    or detail_id
                )
                webhook_sent = await asyncio.to_thread(
                    sellauth_webhook_was_sent, invoice_id
                )
                if initialize_webhook_baseline and not webhook_sent:
                    await asyncio.to_thread(mark_sellauth_webhook_sent, invoice_id)
                elif webhook_url and not webhook_sent:
                    try:
                        await send_sellauth_order_webhook(session, invoice, invoice_id)
                        await asyncio.to_thread(mark_sellauth_webhook_sent, invoice_id)
                        logger.info(
                            "Sent SellAuth completed-order webhook for invoice %s",
                            invoice_id,
                        )
                    except aiohttp.ClientError:
                        logger.exception(
                            "Could not send SellAuth completed-order webhook for invoice %s",
                            invoice_id,
                        )

                discord_user_id = extract_discord_user_id(invoice)
                supporter_keys = extract_supporter_keys(invoice)
                if discord_user_id is None or not supporter_keys:
                    continue

                pending_keys: list[str] = []
                for key in supporter_keys:
                    already_sent = await asyncio.to_thread(
                        sellauth_dm_was_sent, invoice_id, key
                    )
                    if not already_sent:
                        pending_keys.append(key)
                if not pending_keys:
                    continue

                try:
                    user = self.get_user(discord_user_id)
                    if user is None:
                        user = await self.fetch_user(discord_user_id)

                    for key in pending_keys:
                        await send_purchase_key_dm(user, key)
                        await asyncio.to_thread(
                            mark_sellauth_dm_sent,
                            invoice_id,
                            discord_user_id,
                            key,
                        )
                        logger.info(
                            "Sent SellAuth purchase key DM for invoice %s to user %s",
                            invoice_id,
                            discord_user_id,
                        )
                except discord.Forbidden:
                    logger.warning(
                        "Could not DM SellAuth buyer %s; their DMs may be closed",
                        discord_user_id,
                    )
                except (discord.NotFound, discord.HTTPException):
                    logger.exception(
                        "Discord error while delivering SellAuth invoice %s",
                        invoice_id,
                    )

            if initialize_webhook_baseline:
                await asyncio.to_thread(set_bot_setting, webhook_baseline_key, "1")
                logger.info(
                    "Initialized SellAuth order-webhook history; future completed orders will post"
                )

    async def ensure_purchase_panel(self) -> None:
        """Post the store panel once and remember its Discord message ID."""
        if self._purchase_panel_checked:
            return
        self._purchase_panel_checked = True

        channel_id = int(getattr(config, "PURCHASE_CHANNEL_ID", 0) or 0)
        store_url = str(getattr(config, "QUANTUM_STORE_URL", "")).strip()
        if channel_id <= 0 or not store_url:
            logger.warning("Automatic purchase panel is not configured")
            return

        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise TypeError("PURCHASE_CHANNEL_ID must point to a text channel")

        description = (
            "Welcome to Quantum Animal Company Mods — your go-to shop for creative, "
            "high-quality mods designed to make your gameplay more exciting, unique, "
            "and fun! We focus on bringing you mods, new features, custom additions, "
            "and gameplay enhancements that add a fresh experience to your game. "
            "Whether you're looking for something wild, funny, immersive, or completely "
            "different, Quantum Mods Are A Great Choice\n\n"
            "**What We Have?**\n"
            "🐾 **Animal Company Mods**\n"
            "⚡ **Unique Features & Additions**\n"
            "🔧 **Regular Updates & Improvements**\n"
            "💬 **Community-Focused Support**\n"
            "🔑 **Use Your Own Token or One of Ours**\n"
            "🔄 **Fully Automatic Refreshing Tokens**\n\n"
            "Explore the shop, discover your next favourite mod, and take your gameplay "
            "to another level with Quantum Animal Company Mods!\n\n"
            f"💳 **[Purchase Quantum — $8.80]({store_url})**\n"
            f"🎮 **[Purchase Quantum Robux — 1000 Robux]({QUANTUM_ROBUX_STORE_URL})**"
        )

        setting_key = f"purchase_panel_message:{channel_id}"
        saved_message_id = await asyncio.to_thread(get_bot_setting, setting_key)
        if saved_message_id:
            try:
                existing_message = await channel.fetch_message(int(saved_message_id))
                existing_view = discord.ui.View(timeout=None)
                existing_view.add_item(
                    discord.ui.Button(
                        label="Purchase Quantum",
                        style=discord.ButtonStyle.link,
                        url=store_url,
                    )
                )
                existing_view.add_item(
                    discord.ui.Button(
                        label="Purchase Quantum Robux",
                        style=discord.ButtonStyle.link,
                        url=QUANTUM_ROBUX_STORE_URL,
                    )
                )
                if existing_message.embeds:
                    existing_embed = existing_message.embeds[0]
                    existing_embed.title = None
                    existing_embed.description = description
                    existing_embed.url = None
                    existing_embed.colour = discord.Colour.from_rgb(255, 145, 0)
                    existing_embed.set_footer(text="Quantum Animal Company Mods")
                    await existing_message.edit(content=None, embed=existing_embed, view=existing_view)
                elif existing_message.content:
                    await existing_message.edit(content=None, view=existing_view)
                logger.info("Quantum purchase panel already exists in channel %s", channel_id)
                return
            except (ValueError, discord.NotFound):
                pass
        embed = discord.Embed(
            description=description,
            colour=discord.Colour.from_rgb(255, 145, 0),
        )
        embed.set_footer(text="Quantum Animal Company Mods")

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="Purchase Quantum",
                style=discord.ButtonStyle.link,
                url=store_url,
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Purchase Quantum Robux",
                style=discord.ButtonStyle.link,
                url=QUANTUM_ROBUX_STORE_URL,
            )
        )

        send_options: dict[str, Any] = {
            "embed": embed,
            "view": view,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if PURCHASE_BANNER_FILE.exists():
            image_name = "quantum-purchase-banner.png"
            embed.set_image(url=f"attachment://{image_name}")
            send_options["file"] = discord.File(PURCHASE_BANNER_FILE, filename=image_name)
        else:
            logger.warning("Purchase banner is missing: %s", PURCHASE_BANNER_FILE)

        message = await channel.send(**send_options)
        await asyncio.to_thread(set_bot_setting, setting_key, str(message.id))
        logger.info("Posted Quantum purchase panel to channel %s", channel_id)

    async def ensure_support_ticket_panel(self) -> None:
        """Post the supporter-ticket panel once and remember its message ID."""
        if self._support_ticket_panel_checked:
            return
        self._support_ticket_panel_checked = True

        channel_id = int(getattr(config, "SUPPORT_PANEL_CHANNEL_ID", 0) or 0)
        ticket_url = str(getattr(config, "SUPPORT_TICKET_URL", "")).strip()
        if channel_id <= 0 or not ticket_url:
            logger.warning("Automatic supporter-ticket panel is not configured")
            return

        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise TypeError("SUPPORT_PANEL_CHANNEL_ID must point to a text channel")

        description = (
            "Once your purchase is complete, please redeem your supporter key and "
            f"make a ticket here: **[Supporter Ticket]({ticket_url})** and answer "
            "one of our main questions:\n\n"
            "**Do you want to use your own token or one of ours?**\n"
            "⚠️ **Our tokens are VERY LIMITED.**"
        )
        setting_key = f"support_ticket_panel_message:{channel_id}"
        saved_message_id = await asyncio.to_thread(get_bot_setting, setting_key)
        if saved_message_id:
            try:
                existing_message = await channel.fetch_message(int(saved_message_id))
                if existing_message.embeds:
                    existing_embed = existing_message.embeds[0]
                    existing_embed.title = "Quantum Support Ticket"
                    existing_embed.description = description
                    existing_embed.colour = discord.Colour.from_rgb(255, 145, 0)
                    existing_embed.set_footer(text="Quantum Support")
                    await existing_message.edit(content=None, embed=existing_embed)
                elif existing_message.content:
                    await existing_message.edit(content=None)
                logger.info(
                    "Quantum supporter-ticket panel already exists in channel %s",
                    channel_id,
                )
                return
            except (ValueError, discord.NotFound):
                pass

        embed = discord.Embed(
            title="Quantum Support Ticket",
            description=description,
            colour=discord.Colour.from_rgb(255, 145, 0),
        )
        embed.set_footer(text="Quantum Support")
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="Supporter Ticket",
                style=discord.ButtonStyle.link,
                url=ticket_url,
            )
        )

        send_options: dict[str, Any] = {
            "embed": embed,
            "view": view,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if LOGO_FILE.exists():
            image_name = "quantum-support-logo.png"
            embed.set_thumbnail(url=f"attachment://{image_name}")
            send_options["file"] = discord.File(LOGO_FILE, filename=image_name)
        else:
            logger.warning("Support panel logo is missing: %s", LOGO_FILE)

        message = await channel.send(**send_options)
        await asyncio.to_thread(set_bot_setting, setting_key, str(message.id))
        logger.info("Posted Quantum supporter-ticket panel to channel %s", channel_id)

    @tasks.loop(seconds=30)
    async def sellauth_purchase_poll(self) -> None:
        try:
            await self.process_sellauth_purchases()
        except aiohttp.ClientResponseError as error:
            logger.error(
                "SellAuth API returned HTTP %s. Check the API key and shop ID.",
                error.status,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError):
            logger.exception("Could not reach the SellAuth API")
        except Exception:
            logger.exception("Unexpected SellAuth purchase polling error")

    @sellauth_purchase_poll.before_loop
    async def before_sellauth_purchase_poll(self) -> None:
        await self.wait_until_ready()

    async def close(self) -> None:
        if self.sellauth_purchase_poll.is_running():
            self.sellauth_purchase_poll.cancel()
        await super().close()


bot = QuantumBot()


def clean_changelog_text(value: str) -> list[str]:
    """Make user-provided changelog text safe inside an ANSI code block."""
    value = value.replace("\x1b", "").replace("```", "'''").strip()
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return lines or ["Nothing!"]


def ansi_changelog_section(
    heading: str, message: str, symbol: str, color_code: str
) -> str:
    formatted_lines = "\n".join(
        f"\x1b[2;{color_code}m{symbol} {line}\x1b[0m"
        for line in clean_changelog_text(message)
    )
    return f"\x1b[1;37m{heading}\x1b[0m\n{formatted_lines}"


mod_group = app_commands.Group(
    name="mod", description="Quantum Mods update commands"
)


@mod_group.command(name="update", description="Post a Quantum Mods changelog")
@app_commands.describe(
    added="What was added",
    fixed="What was fixed",
    changed="What was changed",
    removed="What was removed",
)
async def mod_update(
    interaction: discord.Interaction,
    added: app_commands.Range[str, 1, 500] = "Nothing!",
    fixed: app_commands.Range[str, 1, 500] = "Nothing!",
    changed: app_commands.Range[str, 1, 500] = "Nothing!",
    removed: app_commands.Range[str, 1, 500] = "Nothing!",
) -> None:
    if not await bot.is_owner(interaction.user):
        await interaction.response.send_message(
            "Only the bot owner can use `/mod update`.", ephemeral=True
        )
        return

    channel_id = int(getattr(config, "MOD_UPDATE_CHANNEL_ID", 0) or 0)
    if channel_id <= 0:
        await interaction.response.send_message(
            "The mod-update channel has not been configured yet.", ephemeral=True
        )
        return

    changelog = "\n\n".join(
        (
            ansi_changelog_section("Added", added, "+", "32"),
            ansi_changelog_section("Fixed", fixed, "!", "33"),
            ansi_changelog_section("Changed", changed, "?", "34"),
            ansi_changelog_section("Removed", removed, "-", "31"),
        )
    )
    embed = discord.Embed(
        title="Quantum Has Updated",
        description=(
            "A new Quantum Mods update is now available. Check out the latest "
            f"changes below.\n\n```ansi\n{changelog}\n```"
        ),
        color=discord.Color.from_rgb(255, 145, 0),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Quantum Mods • Update log")

    try:
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise TypeError("Configured mod-update channel cannot receive messages")

        if LOGO_FILE.exists():
            logo = discord.File(LOGO_FILE, filename="quantum-update-logo.png")
            embed.set_thumbnail(url="attachment://quantum-update-logo.png")
            await channel.send(
                embed=embed,
                file=logo,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            logger.warning("Logo file is missing: %s", LOGO_FILE)
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
    except (discord.Forbidden, discord.NotFound, discord.HTTPException, TypeError):
        logger.exception("Could not send Quantum Mods update")
        await interaction.response.send_message(
            "I could not post the update. Check my channel permissions.",
            ephemeral=True,
        )
        return


    await interaction.response.send_message(
        f"Quantum Mods update posted to <#{channel_id}>.", ephemeral=True
    )


bot.tree.add_command(mod_group)


@bot.tree.command(name="redeem", description="Open the key redemption panel")
@app_commands.guild_only()
async def redeem(interaction: discord.Interaction) -> None:
    if not await bot.is_owner(interaction.user):
        await interaction.response.send_message(
            "Only the bot owner can use `/redeem` to post the redemption panel.",
            ephemeral=True,
        )
        return

    if config.PANEL_CHANNEL_ID <= 0:
        await interaction.response.send_message(
            "The redemption channel has not been configured yet.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title=config.EMBED_TITLE,
        description=config.EMBED_DESCRIPTION,
        color=discord.Color.from_rgb(255, 145, 0),
    )

    try:
        channel = bot.get_channel(config.PANEL_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(config.PANEL_CHANNEL_ID)
        if not isinstance(channel, discord.abc.Messageable):
            raise TypeError("Configured channel cannot receive messages")

        if LOGO_FILE.exists():
            logo = discord.File(LOGO_FILE, filename="quantum-logo.png")
            embed.set_thumbnail(url="attachment://quantum-logo.png")
            await channel.send(embed=embed, view=RedeemView(), file=logo)
        else:
            logger.warning("Logo file is missing: %s", LOGO_FILE)
            await channel.send(embed=embed, view=RedeemView())
    except (discord.Forbidden, discord.NotFound, discord.HTTPException, TypeError):
        logger.exception("Could not send the redemption panel")
        await interaction.response.send_message(
            "I could not send the panel to the configured channel. Make sure the "
            "channel ID is correct and I have **View Channel**, **Send Messages**, "
            "and **Embed Links** there.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Redemption panel sent to <#{config.PANEL_CHANNEL_ID}>.", ephemeral=True
    )


@bot.tree.command(
    name="supportgrant",
    description="DM a user their Quantum Supporter key",
)
@app_commands.guild_only()
@app_commands.describe(
    user="The user to DM the supporter key to",
    key="Supporter key, formatted like 0000-0000-0000",
    paid_via="How the purchase was made",
    credential_id="Credential ID, 16 letters/digits (optional)",
)
@app_commands.choices(
    paid_via=[
        app_commands.Choice(name="Sellauth", value="Sellauth"),
        app_commands.Choice(name="Beta Testing", value="Beta Testing"),
        app_commands.Choice(name="Free Access", value="Free Access"),
    ]
)
async def send_supporter_key(
    interaction: discord.Interaction,
    user: discord.User,
    key: app_commands.Range[str, 1, 14],
    paid_via: app_commands.Choice[str],
    credential_id: app_commands.Range[str, 16, 16] | None = None,
) -> None:
    member = interaction.user
    if not isinstance(member, discord.Member) or not any(
        role.id == SUPPORTER_KEY_DM_ROLE_ID for role in member.roles
    ):
        await interaction.response.send_message(
            "You do not have permission to use this command.", ephemeral=True
        )
        return

    if not SUPPORTER_KEY_DM_INPUT_PATTERN.match(key):
        await interaction.response.send_message(
            "The key must be formatted like `0000-0000-0000` (letters and "
            "numbers allowed).",
            ephemeral=True,
        )
        return

    if credential_id is not None and not CREDENTIAL_ID_DM_INPUT_PATTERN.match(
        credential_id
    ):
        await interaction.response.send_message(
            "The credential ID must be 16 letters/numbers, formatted like "
            "`0000000000000000`.",
            ephemeral=True,
        )
        return

    try:
        await send_supporter_key_confirmation_dm(
            user, key, paid_via.value, interaction.user, credential_id
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            f"Could not DM {user.mention} — they may have DMs disabled.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        logger.exception("Could not send supporter key DM")
        await interaction.response.send_message(
            "Something went wrong sending the DM. Please try again.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Sent the supporter key DM to {user.mention}.", ephemeral=True
    )


@bot.event
async def on_ready() -> None:
    if bot.user:
        logger.info("Logged in as %s (%s)", bot.user, bot.user.id)
    try:
        await bot.ensure_purchase_panel()
    except (discord.Forbidden, discord.HTTPException, TypeError, ValueError):
        bot._purchase_panel_checked = False
        logger.exception(
            "Could not create the automatic Quantum purchase panel. Check the "
            "channel ID and the bot's Send Messages, Embed Links, and Attach Files permissions."
        )
    try:
        await bot.ensure_support_ticket_panel()
    except (discord.Forbidden, discord.HTTPException, TypeError, ValueError):
        bot._support_ticket_panel_checked = False
        logger.exception(
            "Could not create the automatic Quantum supporter-ticket panel. Check the "
            "channel ID and the bot's Send Messages, Embed Links, and Attach Files permissions."
        )


def main() -> None:
    token = config.BOT_TOKEN.strip()
    if not token or token == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Add your Discord bot token to config.py before starting the bot.")
    if config.BUYER_ROLE_ID <= 0:
        raise RuntimeError("Add your buyer role ID to BUYER_ROLE_ID in config.py.")
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
