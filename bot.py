import os
import random
import sqlite3
import threading
from datetime import datetime, timezone

import discord
from discord import app_commands
from flask import Flask, jsonify

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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chat_history (user_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)"
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


def get_chat_history(user_id: int, limit: int = 8):
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM chat_history WHERE user_id = ? ORDER BY rowid DESC LIMIT ?",
            (str(user_id), limit),
        ).fetchall()
    return list(reversed(rows))


def save_chat_turn(user_id: int, user_message: str, bot_reply: str):
    now = datetime.now(timezone.utc).isoformat()
    with connect_db() as conn:
        conn.executemany(
            "INSERT INTO chat_history (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            [
                (str(user_id), "user", user_message[:1500], now),
                (str(user_id), "assistant", bot_reply[:1500], now),
            ],
        )
        # Keep only the latest 30 messages per user.
        conn.execute(
            "DELETE FROM chat_history WHERE user_id = ? AND rowid NOT IN "
            "(SELECT rowid FROM chat_history WHERE user_id = ? ORDER BY rowid DESC LIMIT 30)",
            (str(user_id), str(user_id)),
        )


def clear_chat_history(user_id: int):
    with connect_db() as conn:
        cursor = conn.execute("DELETE FROM chat_history WHERE user_id = ?", (str(user_id),))
        return cursor.rowcount


_last_reply_by_group = {}

def make_reply(message: str, memories: list[str], history=None) -> str:
    original = message.strip()
    text = original.lower()
    if not text:
        return "Bạn chưa nhập câu hỏi kìa 😄"

    history = history or []
    previous_user = next((content for role, content in reversed(history) if role == "user"), "")
    previous_bot = next((content for role, content in reversed(history) if role == "assistant"), "")

    def choose(options):
        key = tuple(options)
        choices = [item for item in options if item != _last_reply_by_group.get(key)]
        selected = random.choice(choices or options)
        _last_reply_by_group[key] = selected
        return selected

    # Safe arithmetic: only numbers and basic arithmetic operators are accepted.
    import ast
    import operator
    import re
    operators = {
        ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
        ast.Pow: operator.pow, ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def calculate(expression):
        def visit(node):
            if isinstance(node, ast.Expression):
                return visit(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return node.value
            if isinstance(node, ast.BinOp) and type(node.op) in operators:
                left, right = visit(node.left), visit(node.right)
                if isinstance(node.op, ast.Pow) and abs(right) > 8:
                    raise ValueError("exponent too large")
                value = operators[type(node.op)](left, right)
                if abs(value) > 10**15:
                    raise ValueError("result too large")
                return value
            if isinstance(node, ast.UnaryOp) and type(node.op) in operators:
                return operators[type(node.op)](visit(node.operand))
            raise ValueError("unsupported expression")

        tree = ast.parse(expression, mode="eval")
        return visit(tree)

    math_match = re.search(r"(?:tính\s*)?([-+]?\d+(?:\.\d+)?(?:\s*[\+\-*/%]\s*[-+]?\d+(?:\.\d+)?)+)", text)
    if math_match:
        try:
            result = calculate(math_match.group(1).replace(" ", ""))
            answer = choose([
                f"Kết quả là **{result}** nhé. 🧮",
                f"Mình tính được: **{result}**. Toán hôm nay chưa thắng được mình rồi! 😎",
                f"Đáp án: **{result}**. Bạn muốn mình tính tiếp phép nào?",
            ])
        except Exception:
            answer = "Phép tính này hơi vượt phạm vi máy tính mini của mình. Bạn kiểm tra lại biểu thức nhé."
    elif any(word in text for word in ("xin chào", "chào", "hello", "hi ", "hey")):
        answer = choose([
            "Chào bạn! Bot 3 đã online, hôm nay mình giúp gì được nào? 😎",
            "Hello! Quăng câu hỏi đây, mình đang nghe. 👋",
            "Chào nha! Bộ não Python đã khởi động, cà phê thì chưa. ☕",
            "Ê chào bạn! Hôm nay Discord có chuyện gì vui?",
        ])
    elif any(phrase in text for phrase in ("bạn là ai", "mày là ai", "cậu là ai")):
        answer = choose([
            "Mình là Bot 3, trợ lý Discord tự xây: nhẹ, vui tính và đang học cách hữu ích hơn. 🤖",
            "Bot 3 đây! Mình chạy bằng Python, có trí nhớ SQLite và một chút khiếu hài hước.",
        ])
    elif any(word in text for word in ("cảm ơn", "thanks", "thank you")):
        answer = choose([
            "Không có gì nha! Cứ gọi mình khi cần. 😄",
            "Giúp được bạn là vui rồi. Có câu nào nữa không?",
            "Đừng khách sáo, mình còn đang cần luyện trình đây! 😎",
        ])
    elif any(word in text for word in ("buồn", "mệt mỏi", "chán nản", "áp lực")):
        answer = choose([
            "Nghe như bạn đang có một ngày khá nặng nề. Muốn kể rõ chuyện gì đang xảy ra không?",
            "Bạn không cần phải giải quyết mọi thứ ngay lập tức đâu. Nghỉ một chút rồi mình cùng gỡ từng việc nhé.",
            "Mình đang nghe đây. Điều gì làm bạn thấy mệt nhất lúc này?",
        ])
    elif any(word in text for word in ("đùa", "kể chuyện cười", "joke", "làm mình cười")):
        answer = choose([
            "Vì sao lập trình viên thích bóng tối? Vì ánh sáng có quá nhiều bug! 😂",
            "Bot hỏi máy chủ: 'Cậu ổn không?' Máy chủ đáp: 'Tớ đang chịu tải... tình cảm.' 🤣",
            "Tại sao con bug không đi học? Vì nó sợ bị debug! 🐛",
        ])
    elif previous_user and any(phrase in text for phrase in ("vậy còn", "thế còn", "còn nó", "còn cái đó", "ý mình là", "nói rõ hơn", "giải thích thêm", "tại sao lại vậy")):
        answer = choose([
            f"À, bạn đang nối tiếp chuyện **{previous_user[:180]}**. Mình chưa có kiến thức mở rộng để khẳng định thêm, nhưng có thể cùng bạn phân tích. Bạn muốn làm rõ phần nào?",
            f"Mình hiểu bạn đang hỏi tiếp ý trước: **{previous_user[:180]}**. Bạn đang muốn biết nguyên nhân, cách làm hay ví dụ cụ thể?",
            f"Để nối đúng mạch: lúc nãy bạn hỏi **{previous_user[:180]}**. Bạn nói rõ phần muốn đào sâu nhé.",
        ])
    elif any(word in text for word in ("làm sao", "cách nào", "hướng dẫn", "bằng cách nào")):
        answer = choose([
            "Mình sẽ giúp bạn chia việc đó thành từng bước. Bạn cho mình biết mục tiêu cụ thể và bạn đang làm trên thiết bị/app nào nhé.",
            "Để hướng dẫn đúng, mình cần biết bạn muốn đạt kết quả gì và hiện đang mắc ở bước nào.",
            "Mình có thể hướng dẫn từng bước. Bạn gửi thêm bối cảnh hoặc thông báo lỗi nếu có nha.",
        ])
    elif any(word in text for word in ("là gì", "nghĩa là gì", "định nghĩa")):
        topic = original.replace("là gì", "").replace("nghĩa là gì", "").replace("định nghĩa", "").strip(" ?")
        answer = choose([
            f"Bạn đang hỏi về **{topic or original}**. Mình chưa có kho kiến thức đầy đủ để xác minh định nghĩa chính xác. Bạn cho mình biết lĩnh vực hoặc ngữ cảnh nhé, mình sẽ giải thích sát hơn.",
            f"Mình cần thêm ngữ cảnh về **{topic or original}** để tránh giải thích sai. Bạn gặp thuật ngữ này ở đâu?",
        ])
    elif any(word in text for word in ("code", "python", "lập trình", "discord bot", "lỗi")):
        answer = choose([
            "Mình có thể giúp phân tích code Python và bot Discord. Gửi đoạn code hoặc lỗi đầy đủ, mình sẽ tìm nguyên nhân cùng bạn.",
            "Được, mình thử gỡ lỗi nhé. Bạn gửi thông báo lỗi và đoạn code liên quan; nhớ che token/mật khẩu.",
            "Mình hỗ trợ lập trình được ở mức hướng dẫn và phân tích. Bạn đang muốn viết tính năng mới hay sửa lỗi?",
        ])
    elif any(word in text for word in ("bạn nghĩ sao", "theo bạn", "nên làm gì", "lời khuyên")):
        answer = choose([
            "Mình nghĩ nên xác định mục tiêu trước, rồi so sánh lợi ích, rủi ro và công sức của từng lựa chọn. Bạn kể thêm tình huống nhé.",
            "Mình có thể cùng bạn cân nhắc. Hiện có những lựa chọn nào và điều gì quan trọng nhất với bạn?",
            "Chưa đủ dữ kiện để kết luận, nhưng mình sẵn sàng phân tích ưu nhược điểm cùng bạn.",
        ])
    elif "?" in original or any(word in text for word in ("tại sao", "vì sao", "bao nhiêu", "khi nào", "ở đâu", "ai ", "có phải")):
        answer = choose([
            "Mình hiểu đây là câu hỏi, nhưng bộ não offline hiện chưa có dữ liệu đủ để xác minh câu trả lời. Bạn thêm một chút bối cảnh nhé, mình sẽ thử phân tích.",
            "Câu này cần kiến thức cụ thể hơn những gì mình được cài sẵn. Mình không muốn bịa đáp án cho bạn đâu. Bạn đang hỏi trong ngữ cảnh nào?",
            "Mình chưa chắc đáp án chính xác. Cho mình thêm chi tiết hoặc một ví dụ để thu hẹp câu hỏi nhé.",
            "Để không trả lời đại, mình cần thêm dữ kiện. Bạn muốn hỏi về phần nào của vấn đề này?",
        ])
    else:
        answer = choose([
            "Mình đang nghe đây. Bạn muốn mình phân tích, giải thích hay đưa ra ý tưởng cho chuyện đó?",
            "Mình chưa bắt được hết ý. Bạn nói rõ hơn một chút nhé, mình sẽ thử giúp.",
            "Có vẻ đây là chủ đề mới với bộ não offline của mình. Kể thêm bối cảnh đi, mình cùng suy luận nhé.",
            "Mình có thể trao đổi tiếp về chuyện này. Phần nào bạn muốn tìm hiểu trước?",
            "Góc nhìn thú vị đó! Bạn muốn đào sâu điểm nào?",
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
    history = get_chat_history(interaction.user.id)
    answer = make_reply(message, memories, history)
    save_chat_turn(interaction.user.id, message, answer)
    await interaction.followup.send(answer[:1900])


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    channel_id = get_ai_channel(message.guild.id)
    if channel_id != message.channel.id:
        return
    memories = get_memories(message.author.id)
    history = get_chat_history(message.author.id)
    answer = make_reply(message.content, memories, history)
    save_chat_turn(message.author.id, message.content, answer)
    await message.reply(answer[:1900], mention_author=False)


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
    history_count = clear_chat_history(interaction.user.id)
    await interaction.response.send_message(
        f"Đã xóa {count} ghi chú và {history_count} tin nhắn hội thoại của bạn."
    )


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
        "Bot 3 dùng bộ xử lý offline, có ghi nhớ hội thoại gần đây và máy tính cơ bản; không cần API key AI.\n"
        "/forget cũng xóa lịch sử trò chuyện của bạn.",
        ephemeral=True,
    )


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    if not TOKEN:
        raise RuntimeError("Thiếu biến môi trường DISCORD_TOKEN. Hãy thêm token bot trong phần Environment/Secrets của host.")
    connect_db().close()
    client.run(TOKEN)
