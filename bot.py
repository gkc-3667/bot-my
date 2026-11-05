import asyncio
import os
import re
import random
import sqlite3
import threading
import ipaddress
import socket
import time
import json
import unicodedata
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
PASTEFY_RAW_URL = os.getenv("PASTEFY_RAW_URL", "").strip()

# Kho kiến thức Pastefy: hỗ trợ tối đa 80.000 Q&A, cache 5 phút.
_pastefy_cache = {"expires": 0.0, "entries": []}
_pastefy_lock = threading.Lock()

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


def normalize_knowledge_text(value: str) -> str:
    value = unicodedata.normalize("NFD", str(value).lower())
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    return value.replace("đ", "d")


def parse_pastefy_knowledge(raw: str) -> list[dict]:
    """Đọc Q&A JSON hoặc văn bản; hỗ trợ từ khóa, bí danh và chủ đề."""
    entries = []
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("items", data.get("qa", data.get("data", [])))
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                question = item.get("question") or item.get("q")
                answer = item.get("answer") or item.get("a")
                if question and answer:
                    aliases = item.get("aliases", item.get("alias", []))
                    tags = item.get("tags", item.get("keywords", []))
                    if isinstance(aliases, str):
                        aliases = [x.strip() for x in re.split(r"[,;|]", aliases) if x.strip()]
                    if isinstance(tags, str):
                        tags = [x.strip() for x in re.split(r"[,;|]", tags) if x.strip()]
                    entries.append({
                        "question": str(question)[:500],
                        "answer": str(answer)[:2500],
                        "aliases": [str(x)[:180] for x in aliases[:12]] if isinstance(aliases, list) else [],
                        "tags": [str(x)[:80] for x in tags[:20]] if isinstance(tags, list) else [],
                    })
            if entries:
                return entries[:80000]
    except (ValueError, TypeError, AttributeError):
        pass

    question = None
    answer_lines = []
    aliases = []
    tags = []

    def flush():
        if question and answer_lines:
            entries.append({
                "question": question[:500],
                "answer": "\n".join(answer_lines).strip()[:2500],
                "aliases": aliases[:12],
                "tags": tags[:20],
            })

    for line in raw.splitlines():
        stripped = line.strip()
        q_match = re.match(r"^(?:Q|Question|Câu hỏi)\s*:\s*(.+)$", stripped, re.I)
        a_match = re.match(r"^(?:A|Answer|Trả lời)\s*:\s*(.*)$", stripped, re.I)
        alias_match = re.match(r"^(?:ALIAS|ALIASES|Bí danh)\s*:\s*(.*)$", stripped, re.I)
        tag_match = re.match(r"^(?:TAGS?|KEYWORDS|Từ khóa)\s*:\s*(.*)$", stripped, re.I)
        if q_match:
            flush()
            question = q_match.group(1).strip()
            answer_lines, aliases, tags = [], [], []
        elif alias_match and question and not answer_lines:
            aliases.extend(x.strip() for x in re.split(r"[,;|]", alias_match.group(1)) if x.strip())
        elif tag_match and question and not answer_lines:
            tags.extend(x.strip() for x in re.split(r"[,;|]", tag_match.group(1)) if x.strip())
        elif a_match and question:
            answer_lines = [a_match.group(1).strip()] if a_match.group(1).strip() else []
        elif question and answer_lines and stripped and stripped != "---":
            answer_lines.append(stripped)
    flush()
    return entries[:80000]

def load_pastefy_knowledge(force: bool = False) -> list[dict]:
    """Nạp tối đa 80.000 Q&A từ nhiều URL Raw (GitHub/Pastefy), cache 5 phút."""
    now = time.monotonic()
    with _pastefy_lock:
        if not force and now < _pastefy_cache["expires"]:
            return _pastefy_cache["entries"]

        raw_urls = os.getenv("KNOWLEDGE_RAW_URLS", "").strip()
        urls = [u.strip() for u in re.split(r"[,;\n]+", raw_urls) if u.strip()]
        if not urls and PASTEFY_RAW_URL:
            urls = [PASTEFY_RAW_URL]
        if not urls:
            return []

        merged = []
        seen = set()
        total_bytes = 0
        for url in urls[:8]:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                host = (parsed.hostname or "").lower()
                allowed = (
                    parsed.scheme == "https"
                    and (host == "pastefy.app" or host.endswith(".pastefy.app")
                         or host == "raw.githubusercontent.com")
                )
                if not allowed:
                    print(f"Knowledge source skipped (unsupported host): {host}")
                    continue
                response = requests.get(
                    url, timeout=(4, 12),
                    headers={"User-Agent": "GKCAI-Knowledge/2.0"},
                    allow_redirects=False,
                )
                response.raise_for_status()
                declared = int(response.headers.get("Content-Length", "0") or 0)
                if declared and declared > 18_000_000:
                    print(f"Knowledge source skipped (over 18 MB): {host}")
                    continue
                body = response.content
                total_bytes += len(body)
                if len(body) > 18_000_000 or total_bytes > 22_000_000:
                    print("Knowledge loading stopped at the safe size limit.")
                    break
                entries = parse_pastefy_knowledge(body.decode(response.encoding or "utf-8", errors="replace"))
                for item in entries:
                    key = normalize_knowledge_text(item.get("question", ""))[:300]
                    if key and key not in seen:
                        seen.add(key)
                        merged.append(item)
                        if len(merged) >= 80000:
                            break
                print(f"Knowledge source loaded: {len(entries)} entries from {host}")
                if len(merged) >= 80000:
                    break
            except Exception as exc:
                print(f"Knowledge source unavailable: {type(exc).__name__}: {exc}")

        if merged:
            _pastefy_cache["entries"] = merged[:80000]
        _pastefy_cache["expires"] = now + (300 if merged else 60)
        print(f"Knowledge loaded: {len(_pastefy_cache['entries'])}/80000 unique Q&A entries")
        return _pastefy_cache["entries"]


def contextualize_followup(message: str, history=None) -> str:
    """Chỉ nối ngữ cảnh khi câu hiện tại thực sự là câu hỏi tiếp nối."""
    history = history or []
    text = message.strip()
    low = normalize_knowledge_text(text)
    markers = (
        "con cai do", "con no", "the con", "vay con", "noi ro hon",
        "giai thich them", "tai sao lai vay", "y minh la", "cach do",
        "no hoat dong the nao", "lam tiep", "tiep di", "vi sao",
        "cu the hon", "cho vi du", "vi du nua",
    )
    # Không gắn câu hỏi ngắn độc lập như "Python là gì?" vào chủ đề cũ.
    is_fragment = any(marker in low for marker in markers)
    if not is_fragment:
        return text
    previous_user = next((content for role, content in reversed(history) if role == "user"), "")
    if previous_user:
        return (previous_user[:300] + " | Câu hỏi tiếp nối: " + text)[:650]
    return text

_knowledge_index = {"source_id": None, "documents": [], "df": {}, "postings": {}}


def find_pastefy_answer(message: str) -> str | None:
    """Tìm Q&A bằng chỉ mục đảo: chỉ chấm các mục có từ khóa trùng, tiết kiệm CPU."""
    entries = load_pastefy_knowledge()
    if not entries:
        return None

    stopwords = {
        "la", "gi", "cua", "cho", "minh", "toi", "ban", "voi", "mot", "nhung",
        "cac", "the", "nao", "sao", "hay", "giup", "ve", "trong", "co", "khong",
        "duoc", "nhe", "a", "thi", "va", "is", "what", "how", "does", "please",
        "an", "to", "of", "in", "on", "for", "with", "vay", "thi",
    }

    def terms(value):
        normalized = normalize_knowledge_text(value)
        return [w for w in re.findall(r"[a-z0-9]{2,}", normalized) if w not in stopwords]

    source_id = id(entries)
    if _knowledge_index["source_id"] != source_id:
        documents = []
        document_frequency = {}
        postings = {}
        for index, item in enumerate(entries):
            searchable = " ".join(
                [item.get("question", "")] +
                item.get("aliases", []) +
                item.get("tags", [])
            )
            doc_terms = terms(searchable)
            doc_tokens = set(doc_terms)
            doc_bigrams = set(zip(doc_terms, doc_terms[1:]))
            tag_tokens = set(terms(" ".join(item.get("tags", []))))
            documents.append((doc_terms, doc_tokens, doc_bigrams, tag_tokens))
            for token in doc_tokens:
                document_frequency[token] = document_frequency.get(token, 0) + 1
                postings.setdefault(token, []).append(index)
        _knowledge_index.update({
            "source_id": source_id,
            "documents": documents,
            "df": document_frequency,
            "postings": postings,
        })

    query_terms = terms(message)
    query_tokens = set(query_terms)
    if not query_tokens:
        return None

    total_docs = max(1, len(entries))
    import math
    idf = {
        token: math.log1p(
            (total_docs - _knowledge_index["df"].get(token, 0) + 0.5)
            / (_knowledge_index["df"].get(token, 0) + 0.5)
        )
        for token in query_tokens
    }
    weighted_total = sum(idf.values()) or 1.0
    query_norm = normalize_knowledge_text(message)
    query_bigrams = set(zip(query_terms, query_terms[1:]))
    candidates = set()
    for token in query_tokens:
        candidates.update(_knowledge_index["postings"].get(token, ()))

    ranked = []
    for index in candidates:
        item = entries[index]
        item_terms, item_tokens, item_bigrams, tag_tokens = _knowledge_index["documents"][index]
        overlap = query_tokens & item_tokens
        if not overlap:
            continue

        coverage = sum(idf[token] for token in overlap) / weighted_total
        precision = len(overlap) / max(1, len(item_tokens))
        f1 = 2 * coverage * precision / max(0.001, coverage + precision)
        question_norm = normalize_knowledge_text(item.get("question", ""))
        aliases = item.get("aliases", [])
        exact_phrase = 1.0 if query_norm in question_norm or question_norm in query_norm else 0.0
        alias_phrase = max(
            (1.0 if normalize_knowledge_text(alias) in query_norm
             or query_norm in normalize_knowledge_text(alias) else 0.0)
            for alias in aliases
        ) if aliases else 0.0
        phrase = len(query_bigrams & item_bigrams) / max(1, len(query_bigrams | item_bigrams))
        tag_hits = len(query_tokens & tag_tokens) / max(1, len(query_tokens))
        score = (
            0.55 * coverage + 0.16 * f1 + 0.14 * phrase
            + 0.10 * exact_phrase + 0.05 * alias_phrase
        )
        score += min(0.06, tag_hits * 0.06)
        ranked.append((min(1.0, score), len(overlap), index, item))

    if not ranked:
        return None
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    best_score, best_overlap, _, best = ranked[0]
    threshold = 0.43 if len(query_tokens) >= 3 else 0.58
    if best_score < threshold or best_overlap < min(2, len(query_tokens)):
        return None

    if len(query_tokens) <= 3 and len(ranked) > 1:
        second_score, second_overlap = ranked[1][0], ranked[1][1]
        if second_score >= threshold and best_score - second_score < 0.025 and second_overlap >= best_overlap:
            return "Mình tìm thấy vài mục khá giống nhau trong kho Pastefy nên chưa muốn chọn bừa. Bạn nói rõ thêm một từ khóa hoặc ngữ cảnh nhé."

    selected = []
    seen_answers = set()
    secondary_floor = max(threshold, best_score * 0.70)
    for score, overlap, _, item in ranked:
        if len(selected) >= 3 or score < secondary_floor:
            continue
        answer = item.get("answer", "").strip()
        fingerprint = normalize_knowledge_text(answer)[:180]
        if not answer or fingerprint in seen_answers:
            continue
        seen_answers.add(fingerprint)
        selected.append((item.get("question", "Thông tin liên quan"), answer, score))

    if not selected:
        return None
    parts = ["📚 **Mình tìm được thông tin phù hợp trong kho kiến thức:**"]
    for question, answer, score in selected:
        safe_answer = answer.replace("@everyone", "@ everyone").replace("@here", "@ here")
        parts.append(f"**{question[:180]}**\n{safe_answer[:780]}")
    if len(selected) > 1:
        parts.append("_Mình ghép các mục liên quan trong Pastefy; hãy hỏi thêm nếu muốn đào sâu một phần._")
    return "\n\n".join(parts)[:1750]

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


def should_auto_search_message(message: str) -> bool:
    """Tìm web dự phòng cho câu hỏi kiến thức chưa có đáp án offline."""
    text = message.strip().lower()
    if len(text) < 12 or len(text) > 350:
        return False
    casual = (
        "xin chào", "chào bạn", "hello", "hey", "cảm ơn", "thanks",
        "haha", "😂", "🤣", "kể chuyện cười", "làm mình cười",
        "bạn khỏe không", "bạn là ai", "nhớ rằng", "ghi nhớ",
    )
    if any(term in text for term in casual):
        return False
    question_markers = (
        "?", "là gì", "tại sao", "vì sao", "khi nào", "ở đâu",
        "bao nhiêu", "ai là", "cách hoạt động", "so sánh",
        "nguyên nhân", "ý nghĩa", "how ", "what ", "why ", "when ",
    )
    return any(term in text for term in question_markers)


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
    """Trò chuyện, tra kho Q&A và tự tìm web khi gặp câu hỏi kiến thức chưa có đáp án offline."""
    history = history or []
    original = message.strip()
    text = original.lower()

    # Chỉ đọc metadata TikTok khi người dùng yêu cầu rõ ràng.
    tiktok_match = re.search(r"https?://(?:www\.)?(?:vm\.)?tiktok\.com/[^\s<>]+", original)
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
        query = re.sub(r"^(tìm|gửi|cho xem|kiếm)\s*(mình|tôi|tớ)?\s*(ảnh|hình ảnh|hình)?\s*(về|của)?\s*", "", original, flags=re.I).strip()
        try:
            images = await asyncio.to_thread(search_images, query or original, 3)
            urls = [item.get("image") for item in images if item.get("image")]
            if urls:
                return "Mình tìm được ảnh liên quan đây 🖼️\n" + "\n".join(urls[:3])
            return "Mình chưa tìm được ảnh phù hợp. Bạn mô tả chủ thể, màu sắc hoặc phong cách nhé."
        except Exception as exc:
            print(f"Image search failed: {type(exc).__name__}: {exc}")
            return "Dịch vụ tìm ảnh đang lỗi hoặc giới hạn truy cập. Bạn thử lại sau nhé."

    # Kỹ năng chắc chắn (tính toán, chẩn đoán lỗi, viết code) được ưu tiên.
    local = make_reply(original, memories, history)
    obvious_offline = (
        bool(offline_expert_reply(original, history))
        or bool(re.fullmatch(r"[\d\s.+\-*/()%]+", original))
        or any(term in text for term in (
            "xin chào", "chào", "hello", "hey", "cảm ơn", "thanks",
            "kể chuyện cười", "làm mình cười", "nhớ rằng", "ghi nhớ",
            "viết code", "viết mã", "tạo code", "tạo mã",
        ))
    )
    if obvious_offline:
        return local[:1800]

    # Chỉ dùng Pastefy khi tìm được câu hỏi đủ tương đồng; không đoán khi điểm thấp.
    try:
        knowledge_query = contextualize_followup(original, history)
        remote_answer = await asyncio.to_thread(find_pastefy_answer, knowledge_query)
        if remote_answer:
            return remote_answer
    except Exception as exc:
        print(f"Knowledge lookup failed: {type(exc).__name__}: {exc}")

    if not (should_search_message(original) or should_auto_search_message(original)):
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



def offline_expert_reply(message: str, history=None):
    """Kỹ năng offline thực dụng: chẩn đoán lỗi, giải thích khái niệm và tạo mẫu code."""
    text = message.strip()
    low = text.lower()
    history = history or []

    error_patterns = [
        (r"ModuleNotFoundError: No module named ['\"]([^'\"]+)", "Thiếu thư viện", "Thêm tên thư viện vào requirements.txt, cài lại dependencies rồi khởi động bot."),
        (r"NameError: name ['\"]([^'\"]+)['\"] is not defined", "Biến hoặc hàm chưa được định nghĩa", "Kiểm tra chính tả tên biến/hàm, phạm vi khai báo và thứ tự chạy."),
        (r"SyntaxError:", "Lỗi cú pháp Python", "Kiểm tra dấu ngoặc, dấu hai chấm và dấu nháy ở dòng được báo lỗi hoặc ngay phía trước."),
        (r"IndentationError:", "Lỗi thụt lề", "Dùng 4 dấu cách nhất quán, tránh trộn tab và spaces."),
        (r"RuntimeError: Thiếu biến môi trường DISCORD_TOKEN", "Bot chưa nhận token Discord", "Trong Environment/Variables của host, tạo DISCORD_TOKEN và dán token vào Value. Không gửi token cho bất kỳ ai."),
        (r"Connection in use|Address already in use|Errno 98", "Cổng đang được tiến trình khác sử dụng", "Không chạy hai server cùng cổng. Dừng tiến trình cũ hoặc dùng PORT mà host cấp."),
        (r"HTTP Error 404|Not Found", "Đường dẫn trả về 404", "Kiểm tra URL và route. Trang gốc và endpoint /health là hai đường dẫn khác nhau."),
        (r"401 Unauthorized|Invalid.*token|Improper token", "Token không hợp lệ", "Tạo lại token trong Discord Developer Portal, cập nhật biến môi trường và khởi động lại. Không đăng token lên chat."),
        (r"403 Forbidden|Missing Permissions", "Bot thiếu quyền", "Cấp View Channel, Send Messages và Read Message History; kiểm tra quyền riêng của kênh."),
    ]
    for pattern, title, fix in error_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            detail = f" ({match.group(1)})" if match.lastindex else ""
            return f"**Mình phân tích lỗi: {title}{detail}.**\n\n**Cách xử lý:** {fix}\n\nNếu vẫn lỗi, gửi 15–30 dòng log quanh traceback; nhớ che token và mật khẩu."

    explainers = [
        (("async", "asyncio", "bất đồng bộ"), "Trong Python, async khai báo hàm bất đồng bộ và await chờ thao tác I/O mà không chặn toàn bộ event loop. Với Discord bot, cách này giúp bot vẫn nhận sự kiện khi đang chờ mạng hoặc database."),
        (("api là gì", "api nghĩa là gì"), "API là cách hai chương trình giao tiếp theo một quy ước. Ví dụ bot gửi yêu cầu đến dịch vụ, nhận dữ liệu JSON rồi dùng dữ liệu đó trả lời thành viên."),
        (("json là gì", "json nghĩa là gì"), "JSON là định dạng dữ liệu dạng key–value và danh sách. Python có module json để đọc và ghi dữ liệu này."),
        (("sqlite", "sqlite là gì", "cơ sở dữ liệu"), "SQLite là cơ sở dữ liệu gọn nhẹ lưu trong một file. Nó phù hợp để bot lưu cấu hình kênh, lịch sử chat và ghi nhớ mà không cần máy chủ database riêng."),
        (("discord intent", "message content intent", "intent là gì"), "Intent là nhóm sự kiện/dữ liệu Discord cho phép bot nhận. Muốn đọc nội dung tin nhắn, cần bật Message Content Intent trong Developer Portal và intents.message_content = True trong code."),
        (("biến môi trường", "environment variable"), "Biến môi trường là cấu hình truyền cho chương trình khi chạy. Dùng nó để giữ DISCORD_TOKEN ngoài mã nguồn, tránh đưa bí mật lên GitHub."),

        (("list là gì", "danh sách trong python", "python list"), "List là danh sách có thứ tự, có thể thay đổi và chứa nhiều kiểu dữ liệu. Ví dụ: numbers = [1, 2, 3]; numbers.append(4). Dùng list khi cần thêm, xóa hoặc duyệt nhiều phần tử."),
        (("dictionary là gì", "dict là gì", "từ điển python", "python dict"), "Dict lưu dữ liệu theo cặp khóa–giá trị. Ví dụ: user = {'name': 'An', 'age': 18}; print(user.get('name')). Dùng get() để đọc khóa an toàn khi khóa có thể không tồn tại."),
        (("tuple là gì", "python tuple"), "Tuple giống list nhưng không thể thay đổi sau khi tạo. Ví dụ: point = (10, 20). Dùng tuple cho nhóm giá trị cố định."),
        (("set là gì", "python set"), "Set là tập hợp không giữ phần tử trùng lặp. Ví dụ: unique = set([1, 1, 2]) cho kết quả {1, 2}. Nó hữu ích để loại trùng và kiểm tra thành viên nhanh."),
        (("hàm trong python", "function là gì", "def là gì"), "Hàm gom một công việc thành phần có thể tái sử dụng. Ví dụ: def add(a, b): return a + b. Gọi bằng add(2, 3); return gửi kết quả về nơi gọi."),
        (("class là gì", "lập trình hướng đối tượng", "oop là gì"), "Class là bản thiết kế để tạo object. Nó gom dữ liệu và hành vi liên quan. Trong Python, __init__ khởi tạo thuộc tính, còn self tham chiếu đến object hiện tại."),
        (("try except là gì", "xử lý ngoại lệ", "exception là gì"), "try/except giúp xử lý lỗi dự kiến mà không làm chương trình dừng đột ngột. Chỉ bắt loại lỗi bạn có thể xử lý; tránh except Exception quá rộng nếu không ghi log và có phương án dự phòng."),
        (("pip là gì", "cài thư viện python"), "pip là công cụ cài gói Python. Ví dụ: python -m pip install discord.py. Với host, ghi tên gói vào requirements.txt để môi trường cài lại sau mỗi lần deploy."),
        (("venv là gì", "virtual environment", "môi trường ảo python"), "venv tạo môi trường Python riêng cho từng dự án, tránh xung đột phiên bản thư viện. Tạo bằng python -m venv .venv; sau đó kích hoạt môi trường rồi cài dependencies."),
        (("git là gì", "github là gì", "git và github"), "Git là hệ thống quản lý phiên bản giúp theo dõi thay đổi code. GitHub là dịch vụ lưu trữ repository Git và hỗ trợ cộng tác, issue, pull request. Commit lưu một mốc thay đổi; push gửi commit lên máy chủ."),
        (("http là gì", "https là gì", "http và https"), "HTTP là giao thức trao đổi dữ liệu web. HTTPS là HTTP được bảo vệ bằng TLS, giúp mã hóa kết nối và xác thực máy chủ. Không gửi token hoặc mật khẩu qua HTTP không mã hóa."),
        (("mã trạng thái http", "http status code", "lỗi 404 là gì", "lỗi 500 là gì"), "Một số mã HTTP thường gặp: 200 thành công; 301/302 chuyển hướng; 400 yêu cầu sai; 401 chưa xác thực; 403 không đủ quyền; 404 không tìm thấy đường dẫn; 429 gửi quá nhiều yêu cầu; 500 lỗi phía máy chủ."),
        (("rate limit là gì", "giới hạn tốc độ", "429 là gì"), "Rate limit giới hạn số yêu cầu trong một khoảng thời gian để bảo vệ dịch vụ. Khi gặp HTTP 429, hãy đọc Retry-After nếu có, chờ rồi thử lại; không gửi vòng lặp liên tục."),
        (("sql injection là gì", "truy vấn sql an toàn"), "SQL injection xảy ra khi ghép trực tiếp dữ liệu người dùng vào câu SQL. Hãy dùng truy vấn tham số hóa, ví dụ conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)), thay vì nối chuỗi."),
        (("event loop là gì", "vòng lặp sự kiện"), "Event loop điều phối coroutine và I/O bất đồng bộ. Trong discord.py, đừng gọi requests.get() đồng bộ trực tiếp trong handler vì nó chặn bot; dùng aiohttp hoặc asyncio.to_thread cho hàm đồng bộ."),
        (("flask là gì", "gunicorn là gì"), "Flask là framework web Python gọn nhẹ để tạo route như /health. Gunicorn là WSGI server chạy ứng dụng Flask trong môi trường triển khai. Discord bot và web health endpoint là hai phần riêng."),
        (("llm là gì", "mô hình ngôn ngữ lớn", "ai offline là gì"), "LLM là mô hình ngôn ngữ lớn được huấn luyện trên dữ liệu để tạo và hiểu văn bản. Một bot Python chỉ dùng if/else và mẫu câu không phải LLM; nó chỉ xử lý những tình huống đã được lập trình. Muốn gần ChatGPT cần mô hình đã huấn luyện và tài nguyên tính toán đáng kể."),
        (("ram là gì", "thiếu ram", "bộ nhớ ram"), "RAM là bộ nhớ làm việc tạm thời của chương trình. Bot Discord nhẹ có thể chạy với ít RAM, nhưng mô hình ngôn ngữ lớn thường cần nhiều GB RAM/VRAM. Giảm kích thước code không thể thay thế tài nguyên cần cho suy luận AI."),
        (("token discord là gì", "discord bot token"), "Discord bot token là bí mật dùng để xác thực bot. Lưu trong biến môi trường DISCORD_TOKEN, không commit lên GitHub, không gửi vào chat hoặc ảnh chụp màn hình. Nếu lộ token, hãy reset ngay trong Developer Portal."),
        (("message content intent", "bật intent đọc tin nhắn"), "Để bot đọc nội dung tin nhắn thường, bật Message Content Intent ở Discord Developer Portal và đặt intents.message_content = True trong code. Bot cũng cần quyền View Channel, Send Messages và Read Message History ở kênh."),
    ]
    for triggers, explanation in explainers:
        if any(trigger in low for trigger in triggers):
            return explanation + "\n\nBạn muốn mình đưa ví dụ Python thực tế cho phần này không?"

    if "tóm tắt" in low and len(text) > 180:
        return "Mình tóm tắt nội dung bạn gửi:\n" + extractive_summary(text, 5)

    if any(term in low for term in ("viết caption", "viết chú thích", "caption giúp")):
        topic = re.sub(r"^.*?(?:viết caption|viết chú thích|caption giúp)\s*", "", text, flags=re.I).strip(" :,-")
        if not topic:
            return "Được chứ 😄 Bạn gửi ảnh hoặc nói chủ đề, phong cách muốn có (hài hước, dễ thương, ngầu hay chuyên nghiệp), mình sẽ soạn caption."
        return (
            f"**Caption gợi ý cho {topic[:120]}**\n"
            f"1. {topic[:100]} — đôi khi điều giản dị lại tạo nên khoảnh khắc đáng nhớ. ✨\n"
            f"2. Không cần quá hoàn hảo, chỉ cần đúng chất của mình. 😎\n"
            f"3. Một chút {topic[:70]}, một chút vui, thế là đủ cho hôm nay. 💫\n"
            "Bạn muốn caption hài hước, thả thính hay ngắn gọn hơn?"
        )

    if any(term in low for term in ("viết thông báo", "soạn thông báo", "viết nội quy")):
        topic = re.sub(r"^.*?(?:viết thông báo|soạn thông báo|viết nội quy)\s*", "", text, flags=re.I).strip(" :,-")
        if not topic:
            return "Mình soạn được nhé. Bạn cho biết thông báo về việc gì, áp dụng từ khi nào và giọng văn nghiêm túc hay thân thiện."
        return (
            f"**THÔNG BÁO**\n\n"
            f"Chào mọi người!\n\n{topic}\n\n"
            "Mong mọi người đọc kỹ và phối hợp thực hiện. Nếu có thắc mắc, hãy liên hệ ban quản trị. "
            "Cảm ơn mọi người! ❤️"
        )

    if any(term in low for term in ("dịch sang tiếng anh", "dịch sang tiếng việt", "translate")):
        return "Bạn gửi đoạn văn cần dịch nhé. Mình sẽ giúp xử lý phần văn bản được cung cấp; với thuật ngữ chuyên ngành, hãy cho biết ngữ cảnh để chọn nghĩa chính xác."

    if any(term in low for term in ("viết code", "viết mã", "tạo code", "tạo mã", "cho code", "cho mình code")):
        if any(term in low for term in ("đọc file", "đọc tệp", "đọc txt")):
            return "Mẫu Python đọc file UTF-8:\n\n    from pathlib import Path\n    path = Path('data.txt')\n    try:\n        print(path.read_text(encoding='utf-8'))\n    except FileNotFoundError:\n        print('Không tìm thấy file:', path)\n    except OSError as exc:\n        print('Không thể đọc file:', exc)\n\nĐổi data.txt thành đường dẫn file của bạn."
        if any(term in low for term in ("ghi file", "lưu file", "tạo file")):
            return "Mẫu Python ghi file UTF-8:\n\n    from pathlib import Path\n    path = Path('data.txt')\n    path.write_text('Xin chào từ GKC-AI!', encoding='utf-8')\n    print('Đã lưu:', path.resolve())\n\nLưu ý: write_text sẽ ghi đè nội dung cũ."
        if any(term in low for term in ("lệnh slash", "slash command", "lệnh discord")):
            return "Mẫu slash command discord.py: import discord; from discord import app_commands; tạo discord.Client và app_commands.CommandTree; dùng @bot.tree.command(name='hello') cho hàm async; trong hàm gọi interaction.response.send_message('Xin chào!'). Đây là khung hướng dẫn, cần ghép vào client hiện tại chứ không tạo client thứ hai."
        return "Mình có thể viết code, nhưng cần biết chức năng cụ thể, ngôn ngữ và nơi chạy (Discord, web hay Python thường). Gửi code hiện tại nếu có, nhớ che token/mật khẩu nhé."

    if history and any(p in low for p in ("nói tiếp", "tiếp đi", "ý đó", "cái vừa nói", "giải thích thêm")):
        last_bot = next((content for role, content in reversed(history) if role == "assistant"), "")
        if last_bot:
            return f'Mình đang nối tiếp câu trả lời trước: "{last_bot[:450]}"\n\nBạn muốn đào sâu nguyên nhân, ví dụ hay cách thực hiện?'
    return None


_last_reply_by_group = {}

def make_reply(message: str, memories: list[str], history=None) -> str:
    original = message.strip()
    text = original.lower()
    if not text:
        return "Bạn chưa nhập câu hỏi kìa 😄"

    history = history or []
    previous_user = next((content for role, content in reversed(history) if role == "user"), "")
    previous_bot = next((content for role, content in reversed(history) if role == "assistant"), "")

    expert_answer = offline_expert_reply(original, history)
    if expert_answer:
        return expert_answer[:1800]

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

def extract_explicit_memory(message: str):
    """Chỉ lưu điều người dùng chủ động yêu cầu bot ghi nhớ."""
    value = message.strip()
    patterns = (
        r"^(?:hãy )?nhớ rằng\s+(.+)$",
        r"^(?:hãy )?ghi nhớ\s+(.+)$",
        r"^lưu vào bộ nhớ rằng\s+(.+)$",
        r"^remember that\s+(.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, value, re.I)
        if match:
            memory = match.group(1).strip(" .!?")
            if 3 <= len(memory) <= 300:
                return memory
    return None


def limit_user_memories(user_id: int, limit: int = 20):
    """Giữ bộ nhớ nhỏ để SQLite không phình vô hạn."""
    with connect_db() as conn:
        conn.execute(
            "DELETE FROM memories WHERE user_id = ? AND rowid NOT IN "
            "(SELECT rowid FROM memories WHERE user_id = ? ORDER BY rowid DESC LIMIT ?)",
            (str(user_id), str(user_id), limit),
        )


def split_discord_message(content: str, limit: int = 1900):
    """Chia câu trả lời dài thành nhiều tin nhắn, ưu tiên ngắt ở dòng."""
    remaining = content.strip()
    chunks = []
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks or ["Mình chưa tạo được câu trả lời."]




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


@client.tree.command(name="napkho", description="Tải lại kho kiến thức và kiểm tra số mục đã nạp")
@app_commands.checks.has_permissions(manage_guild=True)
async def napkho(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    entries = await asyncio.to_thread(load_pastefy_knowledge, True)
    if not entries:
        await interaction.followup.send(
            "Mình chưa nạp được dữ liệu. Hãy kiểm tra KNOWLEDGE_RAW_URLS (tối đa 8 liên kết Raw GitHub/Pastefy) hoặc PASTEFY_RAW_URL.",
            ephemeral=True,
        )
        return
    # Báo số mục thực tế đã đọc, không khẳng định đây là số kiến thức độc lập.
    categories = sum(1 for item in entries if item.get("category"))
    await interaction.followup.send(
        f"📚 Đã nạp **{len(entries):,} bản ghi Q&A** từ Pastefy. "
        f"Giới hạn hiện tại là 80.000 bản ghi, tối đa 8 nguồn, mỗi nguồn 18 MB và tổng 22 MB. "
        "Lưu ý: số bản ghi không đồng nghĩa với số kiến thức độc lập.",
        ephemeral=True,
    )


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    configured_channel = get_ai_channel(message.guild.id)
    if configured_channel != message.channel.id:
        return

    # Phản ứng nhẹ vào tin nhắn người dùng; không gửi thêm tin nhắn rác.
    lowered = message.content.lower()
    if any(word in lowered for word in ("haha", "😂", "🤣", "đùa", "buồn cười")):
        user_reaction = "😂"
    elif any(word in lowered for word in ("cảm ơn", "thanks", "thank you")):
        user_reaction = "❤️"
    elif "?" in message.content or any(word in lowered for word in ("giúp", "lỗi", "sao", "tại sao")):
        user_reaction = "👀"
    else:
        user_reaction = "👋"

    try:
        await message.add_reaction(user_reaction)
    except (discord.Forbidden, discord.HTTPException):
        pass

    try:
        history = get_chat_history(message.author.id, limit=8)
        memories = get_memories(message.author.id, limit=10)
        explicit_memory = extract_explicit_memory(message.content)

        if explicit_memory:
            save_memory(message.author.id, explicit_memory)
            limit_user_memories(message.author.id)
            reply = "🧠 Mình đã ghi nhớ điều bạn vừa dặn. Khi cần, mình sẽ dùng nó trong những cuộc trò chuyện sau nhé!"
        else:
            reply = await smart_reply(message.content, memories, history)

        save_chat_turn(message.author.id, message.content, reply)
        sent_messages = []
        for chunk in split_discord_message(reply):
            sent = await message.reply(
                chunk,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            sent_messages.append(sent)

        # Bot cũng thả reaction vào câu trả lời của chính nó.
        if sent_messages:
            bot_reaction = "✨" if any(mark in reply for mark in ("📚", "🧠", "**")) else "🤖"
            try:
                await sent_messages[-1].add_reaction(bot_reaction)
            except (discord.Forbidden, discord.HTTPException):
                pass

    except discord.Forbidden:
        print("Không đủ quyền đọc/gửi tin nhắn hoặc thả reaction trong kênh AI.")
    except Exception as exc:
        print(f"on_message failed: {type(exc).__name__}: {exc}")
        try:
            await message.reply(
                "Mình vừa gặp trục trặc khi xử lý tin nhắn. Bạn thử gửi lại sau nhé.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "Thiếu DISCORD_TOKEN. Hãy thêm DISCORD_TOKEN vào Environment/.env của Wispbyte."
        )

    # Health endpoint chạy nền; Discord client giữ tiến trình chính hoạt động.
    web_thread = threading.Thread(
        target=run_web,
        name="gkc-health-server",
        daemon=True,
    )
    web_thread.start()

    print("[GKC]-AI đang khởi động Discord client...")
    client.run(TOKEN)
