import asyncio
import os
import re
import random
import sqlite3
import threading
import ipaddress
import socket
from datetime import datetime, timezone

import discord
from dotenv import load_dotenv
from discord import app_commands
from flask import Flask, jsonify
from ddgs import DDGS
import requests
from bs4 import BeautifulSoup

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("BOT_DB_PATH", "bot3_memory.sqlite3")

web = Flask(__name__)


@web.get("/")
def home():
    return "[GKC]-AI is alive!", 200


@web.get("/health")
def health():
    return jsonify(status="ok", bot="[GKC]-AI"), 200


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



def run_web_search(query: str, limit: int = 6):
    """Search the public web without an API key."""
    query = query.strip()[:300]
    if not query:
        return []
    with DDGS(timeout=12) as search:
        return list(search.text(query, max_results=limit))



def fetch_public_page(url: str) -> dict:
    """Fetch a public HTTP(S) page safely, avoiding local/private network targets."""
    from urllib.parse import urlparse
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Chỉ hỗ trợ URL http hoặc https.")
    host = parsed.hostname.lower()
    if host in ("localhost",) or host.endswith((".local", ".internal")):
        raise ValueError("Không truy cập địa chỉ nội bộ.")
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        for item in addresses:
            ip = ipaddress.ip_address(item[4][0])
            if not ip.is_global:
                raise ValueError("Không truy cập địa chỉ IP riêng hoặc nội bộ.")
    except socket.gaierror as exc:
        raise ValueError("Không phân giải được tên miền.") from exc

    response = requests.get(
        url,
        timeout=(5, 12),
        headers={"User-Agent": "GKCAI-Discord-Reader/1.0"},
        stream=True,
        allow_redirects=True,
    )
    response.raise_for_status()
    final = urlparse(response.url)
    if final.scheme not in ("http", "https") or not final.hostname:
        raise ValueError("Đường dẫn chuyển hướng không hợp lệ.")
    final_host = final.hostname.lower()
    if final_host in ("localhost",) or final_host.endswith((".local", ".internal")):
        raise ValueError("Trang chuyển hướng tới địa chỉ nội bộ.")
    try:
        for item in socket.getaddrinfo(final_host, final.port or (443 if final.scheme == "https" else 80)):
            if not ipaddress.ip_address(item[4][0]).is_global:
                raise ValueError("Trang chuyển hướng tới IP riêng hoặc nội bộ.")
    except socket.gaierror as exc:
        raise ValueError("Không phân giải được tên miền đích.") from exc

    content_type = response.headers.get("Content-Type", "").lower()
    if "html" not in content_type and "text/plain" not in content_type:
        response.close()
        raise ValueError("Đường dẫn không phải trang HTML hoặc văn bản.")
    chunks = []
    total = 0
    for chunk in response.iter_content(16384):
        total += len(chunk)
        if total > 1_500_000:
            break
        chunks.append(chunk)
    response.close()
    raw = b"".join(chunks)
    soup = BeautifulSoup(raw, "html.parser")
    for node in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        node.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else final.netloc
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = " ".join(main.stripped_strings)
    text = " ".join(text.split())
    return {"title": title[:200], "url": response.url, "text": text[:12000]}


def extractive_summary(text: str, max_sentences: int = 6) -> str:
    """Create a lightweight extractive summary; it does not invent new facts."""
    import re
    sentences = [s.strip() for s in re.split(r"(?<=[.!?。])\s+", text) if len(s.strip()) > 35]
    if not sentences:
        return text[:1800]
    words = re.findall(r"[a-zA-ZÀ-ỹ0-9]{3,}", text.lower())
    stop = {
        "của", "những", "được", "trong", "với", "một", "cho", "theo", "này",
        "that", "this", "from", "with", "have", "will", "your", "about",
        "the", "and", "for", "are", "was", "you", "but", "not",
    }
    freq = {}
    for word in words:
        if word not in stop:
            freq[word] = freq.get(word, 0) + 1
    ranked = []
    for index, sentence in enumerate(sentences):
        terms = re.findall(r"[a-zA-ZÀ-ỹ0-9]{3,}", sentence.lower())
        score = sum(freq.get(term, 0) for term in terms if term not in stop)
        score = score / max(1, len(terms) ** 0.5)
        ranked.append((score, index, sentence))
    chosen = sorted(ranked, reverse=True)[:max_sentences]
    chosen.sort(key=lambda item: item[1])
    return "\n".join(f"• {sentence}" for _, _, sentence in chosen)[:1800]


async def read_and_summarize(url: str) -> dict:
    return await asyncio.to_thread(fetch_public_page, url)


def should_search_message(message: str) -> bool:
    """Chỉ tìm web khi người dùng chủ động yêu cầu tra cứu."""
    text = message.lower().strip()
    if not text:
        return False

    # Cụm "tìm cách..." thường là xin lời khuyên/hướng dẫn, không phải tìm web.
    if any(phrase in text for phrase in (
        "tìm cách nói chuyện", "tìm cách giải quyết", "tìm cách làm",
        "tìm cách sửa", "tìm cách viết", "tìm cách học",
    )):
        return False

    explicit_search_phrases = (
        "tìm kiếm", "tìm trên mạng", "tìm trên internet", "tìm trên google",
        "tra cứu", "search giúp", "search hộ", "google giúp", "kiếm nguồn",
        "tìm nguồn", "cho mình nguồn", "đưa nguồn", "dẫn nguồn",
        "kiểm tra trên mạng", "xem trên mạng", "tìm thông tin",
        "tìm giúp", "tìm hộ", "hãy tìm", "giúp mình tìm",
        "cập nhật mới nhất", "tin mới nhất", "thông tin mới nhất",
        "giá hiện tại", "giá hôm nay", "tỷ giá hiện tại",
        "thời tiết hôm nay", "lịch thi đấu mới nhất",
    )
    return any(phrase in text for phrase in explicit_search_phrases)


async def inspect_tiktok(url: str) -> str:
    """Read public TikTok metadata without downloading the video."""
    def read_info():
        import yt_dlp
        options = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)
    info = await asyncio.to_thread(read_info)
    title = info.get("title") or "Video TikTok"
    creator = info.get("uploader") or info.get("channel") or "Không rõ"
    description = (info.get("description") or "").strip()
    views = info.get("view_count")
    likes = info.get("like_count")
    parts = [f"**{title[:250]}**", f"Người đăng: {creator}"]
    if views is not None:
        parts.append(f"Lượt xem: {views:,}")
    if likes is not None:
        parts.append(f"Lượt thích: {likes:,}")
    if description:
        parts.append("Mô tả: " + description[:900])
    parts.append("Mình mới đọc được metadata công khai, chưa hiểu nội dung hình/âm thanh của video.")
    return "\n".join(parts)[:1700]


def search_images(query: str, limit: int = 3):
    with DDGS(timeout=12) as search:
        return list(search.images(query.strip()[:200], max_results=limit))


async def smart_reply(message: str, memories: list[str], history=None) -> str:
    """Trò chuyện mặc định; chỉ tra cứu web khi người dùng yêu cầu rõ ràng."""
    history = history or []
    original = message.strip()
    text = original.lower()

    # Chỉ đọc metadata TikTok khi người dùng yêu cầu rõ ràng.
    tiktok_match = re.search(r"https?://(?:www\\.)?(?:vm\\.)?tiktok\\.com/[^\\s<>]+", original)
    tiktok_requests = ("phân tích", "xem giúp", "tóm tắt", "thông tin video", "video này nói gì", "đọc link", "kiểm tra link")
    if tiktok_match and any(phrase in text for phrase in tiktok_requests):
        try:
            return await inspect_tiktok(tiktok_match.group(0).rstrip(".,!?"))
        except Exception as exc:
            print(f"TikTok inspection failed: {type(exc).__name__}: {exc}")
            return "Mình nhận ra link TikTok, nhưng trang đang chặn việc đọc thông tin công khai. Mình chưa thể xem/nghe toàn bộ video này."
    if tiktok_match:
        return "Mình thấy bạn gửi link TikTok rồi 😄 Bạn muốn mình làm gì với link này? Ví dụ: đọc thông tin công khai, tóm tắt metadata hoặc giúp viết caption."

    # Chỉ tìm ảnh khi người dùng yêu cầu rõ ràng.
    if any(phrase in text for phrase in ("tìm ảnh", "tìm hình", "gửi ảnh", "cho xem ảnh", "kiếm ảnh")):
        query = re.sub(r"^(tìm|gửi|cho xem|kiếm)\\s*(mình|tôi|tớ)?\\s*(ảnh|hình ảnh|hình)?\\s*(về|của)?\\s*", "", original, flags=re.I).strip()
        try:
            images = await asyncio.to_thread(search_images, query or original, 3)
            urls = [item.get("image") for item in images if item.get("image")]
            if urls:
                return "Mình tìm được ảnh liên quan đây 🖼️\\n" + "\\n".join(urls[:3])
            return "Mình chưa tìm được ảnh phù hợp. Bạn mô tả chủ thể, màu sắc hoặc phong cách nhé."
        except Exception as exc:
            print(f"Image search failed: {type(exc).__name__}: {exc}")
            return "Dịch vụ tìm ảnh đang lỗi hoặc giới hạn truy cập. Bạn thử lại sau nhé."

    local = make_reply(original, memories, history)
    if not should_search_message(original) or re.fullmatch(r"[\d\s.+\-*/()%]+", original):
        return local[:1800]

    query = original
    if any(term in text for term in ("trend tiktok", "trending tiktok", "xu hướng tiktok", "trend trên tiktok", "đú trend")):
        query = "TikTok trends viral trending topics 2026 " + original

    try:
        results = await asyncio.to_thread(run_web_search, query, 6)
    except Exception as exc:
        print(f"Web search failed: {type(exc).__name__}: {exc}")
        return "Mình chưa kết nối được dịch vụ tìm kiếm. Mình không muốn đoán bừa; bạn thử lại sau nhé."

    if not results:
        return "Mình chưa tìm thấy nguồn đáng tin phù hợp. Bạn thử diễn đạt cụ thể hơn hoặc thêm tên chủ đề nhé."

    # Read up to two public result pages for better context; snippets remain as fallback.
    source_blocks = []
    for item in results[:2]:
        url = (item.get("href") or "").strip()
        if not url:
            continue
        try:
            page = await asyncio.to_thread(fetch_public_page, url)
            if page.get("text"):
                source_blocks.append((page.get("title") or item.get("title") or "Nguồn", page["url"], page["text"]))
        except Exception as exc:
            print(f"Source read skipped: {type(exc).__name__}: {exc}")

    lines = [f"**Mình đã tra cứu về: {original[:180]}**", ""]
    if source_blocks:
        lines.append("**Thông tin chính từ các trang đọc được:**")
        for title, url, body in source_blocks:
            lines.append(f"**{title[:130]}**")
            lines.append(extractive_summary(body, 3)[:650])
            lines.append(f"Nguồn: {url}")
            lines.append("")
    else:
        lines.append("Mình chưa đọc được toàn văn các trang, nên dưới đây là mô tả từ kết quả tìm kiếm:")
        for index, item in enumerate(results[:4], start=1):
            title = (item.get("title") or "Kết quả").strip()
            body = (item.get("body") or "").strip()
            url = (item.get("href") or "").strip()
            if url:
                lines.append(f"**{index}. {title[:130]}**")
                if body:
                    lines.append(body[:330])
                lines.append(url)
                lines.append("")

    lines.append("Mình ưu tiên nguồn có thể kiểm tra; nếu các nguồn mâu thuẫn, hãy mở link để đối chiếu.")
    if memories:
        lines.append("Ghi nhớ liên quan của bạn: " + " • ".join(memories[-2:]))
    return "\n".join(lines)[:1850]


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
            "Chào bạn! [GKC]-AI đã online, hôm nay mình giúp gì được nào? 😎",
            "Hello! Quăng câu hỏi đây, mình đang nghe. 👋",
            "Chào nha! Bộ não Python đã khởi động, cà phê thì chưa. ☕",
            "Ê chào bạn! Hôm nay Discord có chuyện gì vui?",
        ])
    elif any(phrase in text for phrase in ("bạn là ai", "mày là ai", "cậu là ai")):
        answer = choose([
            "Mình là [GKC]-AI — trợ lý Discord của bạn: thân thiện, tò mò, kiên nhẫn và thích giải quyết vấn đề. 🤖",
            "[GKC]-AI đây! Mình có thể trò chuyện, tìm web, đọc trang, tra cứu code và ghi nhớ điều bạn cho phép. 😎",
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
            "Mình đang nghe đây. Ta thử tách vấn đề thành mục tiêu, dữ kiện và cách giải quyết nhé. Bạn muốn bắt đầu từ đâu?",
            "Mình chưa bắt được hết ý. Bạn nói rõ hơn một chút; mình sẽ hỏi đúng phần còn thiếu thay vì đoán mò.",
            "Chủ đề này đáng đào sâu đấy! Kể thêm bối cảnh, mình sẽ cùng bạn phân tích từng bước. 😎",
            "Mình có thể giúp lên ý tưởng, so sánh lựa chọn hoặc tìm nguồn đáng tin. Bạn muốn hướng nào?",
            "Cứ nói tự nhiên nhé. Mình sẽ cố gắng trả lời rõ ràng, trung thực và có chút hài hước khi phù hợp. 😄",
        ])

    if memories:
        answer += "\n\nMình vẫn nhớ: " + " • ".join(memories[-3:])
    return answer

class GKCAI(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()


client = GKCAI()


@client.event
async def on_ready():
    print(f"[GKC]-AI đã đăng nhập: {client.user} (ID: {client.user.id})")


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


@client.tree.command(name="kenhai", description="Đặt kênh hiện tại làm kênh trò chuyện [GKC]-AI")
@app_commands.checks.has_permissions(manage_guild=True)
async def kenhai(interaction: discord.Interaction):
    if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Lệnh này chỉ dùng trong kênh văn bản của máy chủ.", ephemeral=True)
        return
    set_ai_channel(interaction.guild.id, interaction.channel.id)
    await interaction.response.send_message(
        f"Đã đặt {interaction.channel.mention} làm kênh [GKC]-AI. Mọi người có thể nhắn tin trực tiếp tại đây."
    )


@client.tree.command(name="xoakenhai", description="Xóa cài đặt kênh trò chuyện [GKC]-AI")
@app_commands.checks.has_permissions(manage_guild=True)
async def xoakenhai(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Lệnh này chỉ dùng trong máy chủ.", ephemeral=True)
        return
    removed = clear_ai_channel(interaction.guild.id)
    await interaction.response.send_message(
        "Đã xóa cài đặt kênh AI." if removed else "Máy chủ chưa cài kênh AI."
    )


@client.tree.command(name="xoabonho", description="Xóa toàn bộ trí nhớ và lịch sử của tất cả người dùng")
@app_commands.checks.has_permissions(manage_guild=True)
async def xoabonho(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Lệnh này chỉ dùng trong máy chủ.", ephemeral=True)
        return
    with connect_db() as conn:
        memory_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        history_count = conn.execute("SELECT COUNT(*) FROM chat_history").fetchone()[0]
        conn.execute("DELETE FROM memories")
        conn.execute("DELETE FROM chat_history")
    await interaction.response.send_message(
        f"Đã xóa toàn bộ dữ liệu ghi nhớ của bot: {memory_count} ghi chú và {history_count} tin nhắn lịch sử."
    )


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    channel_id = get_ai_channel(message.guild.id)
    if channel_id != message.channel.id:
        return
    async with message.channel.typing():
        memories = get_memories(message.author.id)
        history = get_chat_history(message.author.id)
        answer = await smart_reply(message.content, memories, history)
        save_chat_turn(message.author.id, message.content, answer)
    await message.reply(answer[:1900], mention_author=False)


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    if not TOKEN:
        raise RuntimeError("Thiếu biến môi trường DISCORD_TOKEN. Hãy thêm token bot trong phần Environment/Secrets của host.")
    connect_db().close()
    client.run(TOKEN)
