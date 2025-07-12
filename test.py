"""
Discord Rank Shop Bot — bug‑fixed & ready for 24 / 7 hosting.
Load secrets from environment variables; validates HEX, removes duplicate run, and uses
an async file‑lock to avoid race conditions.

Required ENV keys:
  DISCORD_BOT_TOKEN   – your Discord bot token
  ADMIN_CHANNEL_ID    – channel ID to receive admin order embeds
  BUYER_CHANNEL_ID    – channel ID to notify buyers
  ALLOWED_USER_ID     – Discord user‑id that can run admin slash‑commands
  TRUEWALLET_API_KEY  – API key for planariashop TrueWallet service (optional)
  TRUEWALLET_PHONE    – phone number bound to that service (optional)

Run with:  python discord_rank_shop_bot.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

# ──────────────────────────────── CONFIG ──────────────────────────────────────
load_dotenv()  # read .env file if present

TOKEN: str | None = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN is not set – create a .env file or export the variable.")

ADMIN_CHANNEL_ID: int = int(os.getenv("ADMIN_CHANNEL_ID", "0"))
BUYER_CHANNEL_ID: int = int(os.getenv("BUYER_CHANNEL_ID", "0"))
ALLOWED_USER_ID: int = int(os.getenv("ALLOWED_USER_ID", "0"))

TRUEWALLET_API_KEY: str = os.getenv("TRUEWALLET_API_KEY", "")
TRUEWALLET_PHONE: str = os.getenv("TRUEWALLET_PHONE", "")

DATA_FILE = Path("data.json")
HEX_RE = re.compile(r"^#?[0-9A-Fa-f]{6}$")

# ──────────────────────────────── BOT SETUP ───────────────────────────────────
intents = discord.Intents.default()
intents.guilds = True
intents.members = True  # needed to add roles

bot = commands.Bot(command_prefix="/", intents=intents)
file_lock = asyncio.Lock()  # protect file writes

# ────────────────────────────── DATA HELPERS ──────────────────────────────────

def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf‑8"))
        except json.JSONDecodeError:
            print("⚠️ data.json is corrupted – starting with a fresh structure.")
    return {"products": [], "orders": [], "users": {}, "topup_logs": []}


data = load_data()


async def save_data() -> None:
    """Atomically write the in‑memory data to disk."""
    async with file_lock:
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf‑8")
        tmp.replace(DATA_FILE)


# ────────────────────────── PERMISSION UTILITIES ─────────────────────────────

def is_admin(user: discord.abc.User) -> bool:
    return user.id == ALLOWED_USER_ID


# ────────────────────────────── TRUEWALLET API ───────────────────────────────

async def check_topup(slip_url: str) -> dict | None:
    if not TRUEWALLET_API_KEY:
        return None
    async with aiohttp.ClientSession() as sess:
        async with sess.get(
            "https://www.planariashop.com/api/truewallet.php",
            params={
                "apikey": TRUEWALLET_API_KEY,
                "url": slip_url,
                "phone": TRUEWALLET_PHONE,
            },
        ) as r:
            if r.status == 200:
                return await r.json()
            return None


# ────────────────────────────── UI COMPONENTS ───────────────────────────────

class ProductSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=p["name"], description=f"ราคา {p['price']} บาท", emoji=p["emoji"])
            for p in data["products"]
        ]
        super().__init__(placeholder="เลือกซื้อยศเลยดิ 📖", options=options, custom_id="select_product")

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("```คุณไม่มีสิทธิ์ใช้คำสั่งนี้```", ephemeral=True)
            return
        product = next((p for p in data["products"] if p["name"] == self.values[0]), None)
        if not product:
            await interaction.response.send_message("```ไม่พบสินค้าในระบบ```", ephemeral=True)
            return
        uid = str(interaction.user.id)
        user_data = data["users"].setdefault(uid, {"balance": 0})
        if user_data["balance"] < product["price"]:
            await interaction.response.send_message("```ยอดเงินไม่เพียงพอ```", ephemeral=True)
            return
        user_data["balance"] -= product["price"]
        await save_data()
        role_name = product["rank"]
        guild = interaction.guild  # type: ignore[assignment]
        role = discord.utils.get(guild.roles, name=role_name)
        if role is None:
            role = await guild.create_role(name=role_name)
        await interaction.user.add_roles(role)
        await interaction.response.send_message(f"**✅ ซื้อยศ `{role_name}` เรียบร้อยแล้ว!**", ephemeral=True)


class OrderButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="สั่งออเดอร์ยศ", style=discord.ButtonStyle.primary, custom_id="btn_order", emoji="📥"
        )

    async def callback(self, interaction):  # type: ignore[override]
        if not is_admin(interaction.user):
            await interaction.response.send_message("```คุณไม่มีสิทธิ์ใช้คำสั่งนี้```", ephemeral=True)
            return
        await interaction.response.send_modal(OrderModal())


class TopupButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="เติมเงิน", style=discord.ButtonStyle.success, emoji="🧧")

    async def callback(self, interaction):  # type: ignore[override]
        await interaction.response.send_modal(TopupModal())


class BalanceButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="เช็คยอด", style=discord.ButtonStyle.secondary, emoji="💰")

    async def callback(self, interaction):  # type: ignore[override]
        bal = data["users"].get(str(interaction.user.id), {}).get("balance", 0)
        await interaction.response.send_message(f"**💰 ยอดเงินคงเหลือ: {bal} บาท**", ephemeral=True)


class ShopView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ProductSelect())
        self.add_item(OrderButton())
        self.add_item(TopupButton())
        self.add_item(BalanceButton())


# ────────────────────────────── MODALS ───────────────────────────────────────

class TopupModal(discord.ui.Modal, title="เติมเงินผ่านซองทรู"):
    link = discord.ui.TextInput(label="วางลิงก์ซองทรูได้เลย 🧧")

    async def on_submit(self, interaction: discord.Interaction):
        result = await check_topup(self.link.value)
        if not result or result.get("status") != "success":
            await interaction.response.send_message("```❌ ลิงก์ซองไม่ถูกต้อง```", ephemeral=True)
            return
        amount = result.get("amount", 0)
        uid = str(interaction.user.id)
        data["users"].setdefault(uid, {"balance": 0})["balance"] += amount
        data["topup_logs"].append({"user_id": uid, "amount": amount, "link": self.link.value})
        await save_data()
        await interaction.response.send_message(f"```✅ เติมเงินสำเร็จ +{amount} บาท```", ephemeral=True)


class OrderModal(discord.ui.Modal, title="สั่งออเดอร์ยศ"):
    rank = discord.ui.TextInput(label="ชื่อยศที่ต้องการ")
    color = discord.ui.TextInput(label="รหัสสี HEX", placeholder="#ff66cc")

    async def on_submit(self, interaction: discord.Interaction):
        # validate input
        if not HEX_RE.fullmatch(self.color.value):
            await interaction.response.send_message("```❌ รหัสสีไม่ถูกต้อง (ตัวอย่าง #ff66cc)```", ephemeral=True)
            return
        uid = str(interaction.user.id)
        bal = data["users"].get(uid, {}).get("balance", 0)
        price = 50  # flat price for custom rank
        if bal < price:
            await interaction.response.send_message("```❌ เงินไม่พอ (50 บาท)```", ephemeral=True)
            return
        data["users"][uid]["balance"] -= price
        order = {
            "order_id": len(data["orders"]) + 1,
            "user_id": uid,
            "rank_name": self.rank.value,
            "color": self.color.value if self.color.value.startswith("#") else f"#{self.color.value}",
            "price": price,
            "status": "รออนุมัติ",
        }
        data["orders"].append(order)
        await save_data()

        embed = discord.Embed(
            title="📥 ออเดอร์ใหม่",
            description=(
                f"👤 คนสั่ง: <@{uid}>\n"
                f"🏷️ ชื่อยศที่สั่ง: {self.rank.value}\n"
                f"💸 จำนวนเงิน: {price} บาท\n"
                f"⏳ สถานะ: รออนุมัติ"
            ),
            color=0xFFCC00,
        )
        embed.set_image(url="https://i.imgur.com/pTT8u8Y.png")
        view = ApprovalView(order)
        admin_ch = bot.get_channel(ADMIN_CHANNEL_ID)
        if admin_ch:
            await admin_ch.send(embed=embed, view=view)
        await interaction.response.send_message("**⏰ ส่งไปแล้ว รอแอดมินอนุมัติ!**", ephemeral=True)


# ────────────────────────── ADMIN APPROVAL VIEW ──────────────────────────────

class ApprovalView(discord.ui.View):
    def __init__(self, order: dict):
        super().__init__(timeout=None)
        self.order = order

    @discord.ui.button(label="อนุญาต", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        guild = interaction.guild  # type: ignore[assignment]
        role_name = f"𐙚 ˚{self.order['rank_name']}ᡣ"
        try:
            role_color = discord.Colour.from_str(self.order["color"])
        except ValueError:
            role_color = discord.Colour.default()
        role = await guild.create_role(name=role_name, colour=role_color)
        user = guild.get_member(int(self.order["user_id"]))
        if user:
            await user.add_roles(role)
        self.order["status"] = "อนุมัติแล้ว"
        await save_data()

        buyer_channel = bot.get_channel(BUYER_CHANNEL_ID)
        if buyer_channel:
            embed = discord.Embed(
                title="✅ ออเดอร์อนุญาตแล้ว",
                description=(
                    f"👤 คนสั่ง: <@{self.order['user_id']}>\n"
                    f"🏷️ ชื่อยศที่สั่ง: {self.order['rank_name']}\n"
                    f"💸 จำนวนเงิน: {self.order['price']} บาท\n"
                    f"✅ สถานะ: อนุญาตแล้ว"
                ),
                color=discord.Color.green(),
            )
            embed.set_image(url="https://i.imgur.com/pTT8u8Y.png")
            await buyer_channel.send(embed=embed)

        await interaction.response.send_message("✅ สร้างยศและเพิ่มให้แล้ว", ephemeral=True)

    @discord.ui.button(label="ปฏิเสธ", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        uid = self.order["user_id"]
        data["users"].setdefault(uid, {"balance": 0})["balance"] += self.order["price"]
        self.order["status"] = "ปฏิเสธ"
        await save_data()

        buyer_channel = bot.get_channel(BUYER_CHANNEL_ID)
        if buyer_channel:
            embed = discord.Embed(
                title="❌ ออเดอร์ถูกปฏิเสธ",
                description=(
                    f"👤 คนสั่ง: <@{uid}>\n"
                    f"🏷️ ชื่อยศที่สั่ง: {self.order['rank_name']}\n"
                    f"💸 จำนวนเงิน: {self.order['price']} บาท\n"
                    f"🚫 สถานะ: ปฏิเสธ"
                ),
                color=discord.Color.red(),
            )
            embed.set_image(url="https://i.imgur.com/pTT8u8Y.png")
            await buyer_channel.send(embed=embed)

        await interaction.response.send_message("```❌ ยกเลิกออเดอร์และคืนเงินแล้ว```", ephemeral=True)


# ────────────────────────────── SLASH COMMANDS ───────────────────────────────

@bot.tree.command(name="setup")
async def setup_cmd(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("```คุณไม่มีสิทธิ์ใช้คำสั่งนี้```", ephemeral=True)
        return
    embed = discord.Embed(
        title="**𝔓𝔞𝔧𝔬𝔯𝔖𝔱𝔞𝔢𝔯**",
        description="**💎 บอทขายยศ + ยศส่วนตัว**\n```🛑 รออนุญาตจากแอดมิน```",
        color=0x9B59B6,
    )
    embed.set_image(url="https://i.imgur.com/pTT8u8Y.png")
    await interaction.response.send_message(embed=embed, view=ShopView())


@bot.tree.command(name="เพิ่มสินค้ายศ")
async def add_product(interaction: discord.Interaction, emoji: str, name: str, rank: str, price: int):
    if not is_admin(interaction.user):
        await interaction.response.send_message("คุณไม่มีสิทธิ์ใช้คำสั่งนี้", ephemeral=True)
        return
    if any(p["name"] == name for p in data["products"]):
        await interaction.response.send_message("ชื่อสินค้านี้มีอยู่แล้ว", ephemeral=True)
        return
    data["products"].append({"emoji": emoji, "name": name, "rank": rank, "price": price, "image": ""})
    await save_data()
    await interaction.response.send_message(f"✅ เพิ่มสินค้ายศ {name} เรียบร้อย", ephemeral=True)


@bot.tree.command(name="ลบสินค้ายศ")
async def remove_product(interaction: discord.Interaction, name: str):
    if not is_admin(interaction.user):
        await interaction.response.send_message("คุณไม่มีสิทธิ์ใช้คำสั่งนี้", ephemeral=True)
        return
    before = len(data["products"])
    data["products"] = [p for p in data["products"] if p["name"] != name]
    await save_data()
    if len(data["products"]) == before:
        await interaction.response.send_message("ไม่พบสินค้าที่จะลบ", ephemeral=True)
    else:
        await interaction.response.send_message(f"✅ ลบสินค้ายศ {name} เรียบร้อย", ephemeral=True)


@bot.tree.command(name="เพิ่มเงิน")
async def add_balance(interaction: discord.Interaction, user: discord.User, amount: int):
    if not is_admin(interaction.user):
        await interaction.response.send_message("คุณไม่มีสิทธิ์ใช้คำสั่งนี้", ephemeral=True)
        return
    uid = str(user.id)
    data["users"].setdefault(uid, {"balance": 0})["balance"] += amount
    await save_data()
    await interaction.response.send_message(f"✅ เพิ่มเงิน {amount} บาท ให้ {user.mention}", ephemeral=True)


@bot.tree.command(name="ลดเงิน")
async def remove_balance(interaction: discord.Interaction, user: discord.User, amount: int):
    if not is_admin(interaction.user):
        await interaction.response.send_message("คุณไม่มีสิทธิ์ใช้คำสั่งนี้", ephemeral=True)
        return
    uid = str(user.id)
    bal = data["users"].setdefault(uid, {"balance": 0})["balance"]
    data["users"][uid]["balance"] = max(0, bal - amount)
    await save_data()
    await interaction.response.send_message(f"✅ ลดเงิน {amount} บาท จาก {user.mention}", ephemeral=True)


# ──────────────────────────────── EVENTS ─────────────────────────────────────

@bot.event
async def on_ready():
    await bot.tree.sync()
    await bot.change_presence(activity=discord.Game(name="เม็ดม่วง"))
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


# ──────────────────────────────── ENTRYPOINT ─────────────────────────────────

if __name__ == "__main__":
    bot.run(TOKEN)
