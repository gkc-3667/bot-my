import os
import random
import sqlite3
import threading
from datetime import datetime, timezone

import discord
from discord import app_commands
from flask import Flask, jsonify
from ai_engine import generate_reply

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("BOT_DB_PATH", "bot3_memory.sqlite3")

web = Flask(__name__)


@web.get("/")
def home():
    return "Bot 3 is alive!", 200


@web.get("/health")
def health():
    return jsonify(status="ok", bot="Bot 3"), 200


def run_web():
    web.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), use_reloader=False)


def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memories (user_id TEXT NOT NULL, memory TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ai_channels (guild_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL)"
    )
    conn.commit()
    return conn


def save_memory(user_id: int, memory: str):
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO memories (user_id, memory, created_at) VALUES (?, ?, ?)",
            (str(user_id), memory, datetime.now(timezone.utc).isoformat()),
        )


def get_memories(user_id: int, limit: int = 10):
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT memory FROM memories WHERE user_id = ? ORDER BY rowid DESC LIMIT ?",
            (str(user_id), limit),
        ).fetchall()
    return [row[0] for row in reversed(rows)]


def delete_memories(user_id: int):
    with connect_db() as conn:
        cursor = conn.execute("DELETE FROM memories WHERE user_id = ?", (str(user_id),))
        return cursor.rowcount


def make_reply(message: str, memories: list[str]) -> str:
    text = message.lower().strip()
    if any(word in text for word in ("xin chào", "chào", "hello", "hi ", "hey")):
        answer = random.choice([
            "Chào bạn! Hôm nay mình hoạt động bằng 100% sự dễ thương và 2% pin tưởng tượng. 😎",
            "Hello! Có chuyện gì kể mình nghe xem nào. 👋",
            "Chào nha! Bot 3 đã có mặt, chưa có cà phê nhưng vẫn tỉnh. ☕",
        ])
    elif "bạn là ai" in text or "mày là ai" in text:
        answer = "Mình là Bot 3 — trợ lý Discord vừa cố tỏ ra thông minh vừa không quên pha trò. 🤖"
    elif "cảm ơn" in text or "thanks" in text:
        answer = "Không có gì! Giúp được bạn là mình vui rồi. 😄"
    elif "buồn" in text or "mệt" in text:
        answer = "Nghe có vẻ hôm nay không dễ dàng lắm. Nghỉ một chút nhé, nếu muốn thì kể mình nghe."
    elif "help" in text or "giúp" in text:
        answer = "Bạn có thể trò chuyện với mình bằng /chat, lưu điều cần nhớ bằng /remember và xem lại bằng /profile."
    else:
        answer = random.choice([
            "Nghe cũng thú vị đó. Kể thêm cho mình nghe đi!",
            "Mình đang xử lý... bằng bộ não Python phiên bản tiết kiệm. 😅",
            "Ồ! Rồi chuyện tiếp theo thế nào?",
            "Mình chưa hiểu hết ý, nhưng mình đang lắng nghe đây.",
            "Nếu đây là bài kiểm tra độ thông minh thì mình xin thêm 3 giây suy nghĩ. 🤔",
        ])
    if memories:
        answer += "\n\nMình vẫn nhớ: " + " • ".join(memories[-3:])
    return answer


class Bot3(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()


client = Bot3()


@client.event
async def on_ready():
    print(f"Bot 3 đã đăng nhập: {client.user} (ID: {client.user.id})")


def set_ai_channel(guild_id: int, channel_id: int):
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO ai_channels (guild_id, channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id",
            (str(guild_id), str(channel_id)),
        )


def get_ai_channel(guild_id: int):
    with connect_db() as conn:
        row = conn.execute(
            "SELECT channel_id FROM ai_channels WHERE guild_id = ?", (str(guild_id),)
        ).fetchone()
    return int(row[0]) if row else None


def clear_ai_channel(guild_id: int):
    with connect_db() as conn:
        cursor = conn.execute(
            "DELETE FROM ai_channels WHERE guild_id = ?", (str(guild_id),)
        )
        return cursor.rowcount > 0


@client.tree.command(name="set_ai_channel", description="Đặt kênh tự động trò chuyện với Bot 3")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(channel="Kênh văn bản dùng làm kênh AI")
async def set_ai_channel_command(interaction: discord.Interaction, channel: discord.TextChannel):
    if interaction.guild is None:
        await interaction.response.send_message("Lệnh này chỉ dùng trong máy chủ.", ephemeral=True)
        return
    set_ai_channel(interaction.guild.id, channel.id)
    await interaction.response.send_message(
        f"Đã đặt {channel.mention} làm kênh AI. Mọi tin nhắn mới trong kênh đó sẽ được Bot 3 trả lời."
    )


@client.tree.command(name="delete_ai_channel", description="Tắt kênh AI của máy chủ")
@app_commands.checks.has_permissions(administrator=True)
async def delete_ai_channel_command(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Lệnh này chỉ dùng trong máy chủ.", ephemeral=True)
        return
    removed = clear_ai_channel(interaction.guild.id)
    message = "Đã xóa cài đặt kênh AI." if removed else "Máy chủ chưa đặt kênh AI."
    await interaction.response.send_message(message)


@client.tree.command(name="chat", description="Trò chuyện với Bot 3")
@app_commands.describe(message="Điều bạn muốn nói với Bot 3")
async def chat(interaction: discord.Interaction, message: str):
    await interaction.response.defer(thinking=True)
    memories = get_memories(interaction.user.id)
    answer = await generate_reply(interaction.user.id, message, memories)\n    await interaction.followup.send(answer[:1900])


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    channel_id = get_ai_channel(message.guild.id)
    if channel_id != message.channel.id:
        return
    memories = get_memories(message.author.id)
    try:\n        answer = await generate_reply(message.author.id, message.content, memories)\n        await message.reply(answer[:1900], mention_author=False)\n    except RuntimeError as exc:\n        await message.reply(f\"{exc}\", mention_author=False)


@client.tree.command(name="remember", description="Bảo Bot 3 ghi nhớ một điều")
@app_commands.describe(note="Điều Bot 3 cần ghi nhớ")
async def remember(interaction: discord.Interaction, note: str):
    note = note.strip()
    if not note:
        await interaction.response.send_message("Bạn chưa nhập điều cần nhớ.", ephemeral=True)
        return
    if len(note) > 500:
        await interaction.response.send_message("Ghi chú dài quá; tối đa 500 ký tự nhé.", ephemeral=True)
        return
    save_memory(interaction.user.id, note)
    await interaction.response.send_message("Đã ghi nhớ điều này rồi nhé! 🧠")


@client.tree.command(name="forget", description="Xóa những điều Bot 3 đã nhớ về bạn")
async def forget(interaction: discord.Interaction):
    count = delete_memories(interaction.user.id)
    await interaction.response.send_message(f"Đã xóa {count} ghi chú của bạn.")


@client.tree.command(name="profile", description="Xem những điều Bot 3 đang nhớ về bạn")
async def profile(interaction: discord.Interaction):
    memories = get_memories(interaction.user.id, limit=20)
    if not memories:
        await interaction.response.send_message(
            "Mình chưa lưu gì về bạn cả. Dùng /remember để bảo mình ghi nhớ nhé.",
            ephemeral=True,
        )
        return
    lines = "\n".join(f"• {item}" for item in memories)
    await interaction.response.send_message("Đây là những điều mình nhớ:\n" + lines, ephemeral=True)


@client.tree.command(name="help", description="Xem hướng dẫn sử dụng Bot 3")
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Bot 3 — hướng dẫn\n"
        "/chat message: Trò chuyện với bot.\n"
        "/remember note: Lưu một điều vào trí nhớ riêng của bạn.\n"
        "/profile: Xem các ghi chú bot đã lưu về bạn.\n"
        "/forget: Xóa toàn bộ ghi chú của bạn.\n"
        "/help: Hiện hướng dẫn này.\n"
        "/set_ai_channel channel: Đặt kênh tự động trả lời (quản trị viên).\n"
        "/delete_ai_channel: Xóa kênh AI (quản trị viên).\n\n"
        "AI hội thoại dùng mô hình chạy trên máy chủ bên ngoài. Cần cấu hình GROQ_API_KEY trên host.",
        ephemeral=True,
    )


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    if not TOKEN:
        raise RuntimeError("Thiếu biến môi trường DISCORD_TOKEN. Hãy thêm token bot trong phần Environment/Secrets của host.")
    connect_db().close()
    client.run(TOKEN)
