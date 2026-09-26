"""
search_module.py — Backend tìm kiếm web (Google/Facebook/YouTube) dùng bởi
agent mode (function-calling trong agent_tools.py) và Google Search
Grounding gốc của Gemini.

  • Google  → DDG + Bing chạy song song/race, vi-VN + en-US, kèm Google News
  • Facebook→ qua Google site:facebook.com, vi-VN
  • YouTube → đa ngôn ngữ, AI tự chọn video phù hợp trong pool kết quả

Việc "có cần search hay không" giờ do Gemini tự quyết định trực tiếp qua
function-calling (xem agent_tools.py: tool_search_web, tool_search_youtube)
hoặc qua Google Search Grounding gốc (ai_module.py: _gemini_chat_with_grounding)
— không còn lớp quyết định/scrape trung gian riêng (/autosearch đã bị gỡ).
"""

import asyncio
import re
from urllib.parse import unquote

import httpx

from config import CONFIG, now_vn
from ai_module import GEMINI_AVAILABLE, _get_all_ai_keys

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    genai = None
    genai_types = None

# ── User-Agent ────────────────────────────────────────────────────
_SEARCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

def _slog(*args) -> None:
    if CONFIG.get("search_debug", False):
        ts = now_vn().strftime("%H:%M:%S")
        print(f"[SEARCH-DBG {ts}]", *args, flush=True)

# ══════════════════════════════════════════════════════════════════
# WEB SEARCH BACKENDS
# ══════════════════════════════════════════════════════════════════

def _strip_tags(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&quot;", '"').replace("&#039;", "'")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&#x27;", "'").replace("&apos;", "'"))
    return re.sub(r"\s{2,}", " ", text).strip()

def _make_result(title: str, url: str, snippet: str = "") -> dict:
    return {"title": _strip_tags(title), "url": url, "snippet": _strip_tags(snippet)}

def _clean_html(raw: str) -> str:
    return _strip_tags(raw)

def _unwrap_ddg_url(url: str) -> str:
    """DDG HTML trả link dạng //duckduckgo.com/l/?uddg=<encoded>&rut=... → giải mã ra link gốc."""
    if "uddg=" in url:
        m = re.search(r'uddg=([^&]+)', url)
        if m:
            return unquote(m.group(1))
    if url.startswith("//"):
        return "https:" + url
    return url

async def _backend_ddg(query: str, max_results: int, client: httpx.AsyncClient) -> list:
    _slog(f"DDG → query={repr(query)}, max={max_results}")
    resp = await client.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query, "kl": "vn-vi"},  # khoá vùng/ngôn ngữ kết quả về Việt Nam
        headers={"User-Agent": _SEARCH_UA},
        timeout=8,
    )
    _slog(f"DDG ← status={resp.status_code}, len={len(resp.text)}")
    links    = re.findall(r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>', resp.text, re.DOTALL)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
    out = []
    for i, (url, title) in enumerate(links[:max_results]):
        snip = snippets[i] if i < len(snippets) else ""
        t = _strip_tags(title)
        if t:
            out.append(_make_result(t, _unwrap_ddg_url(url), snip))
    _slog(f"DDG ✓ {len(out)} kết quả")
    return out

async def _backend_bing(query: str, max_results: int, client: httpx.AsyncClient) -> list:
    _slog(f"Bing → query={repr(query)}, max={max_results}")
    resp = await client.get(
        "https://www.bing.com/search",
        params={"q": query, "count": str(max_results), "mkt": "vi-VN", "cc": "VN", "setlang": "vi"},
        headers={"User-Agent": _SEARCH_UA, "Accept-Language": "vi-VN,vi;q=0.9"},
        timeout=8,
    )
    titles   = re.findall(r'<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
    snippets = re.findall(r'<p class="b_lineclamp[^"]*"[^>]*>(.*?)</p>', resp.text, re.DOTALL)
    out = []
    for i, (url, title) in enumerate(titles[:max_results]):
        if "bing.com" in url and "/search" in url:
            continue
        snip = snippets[i] if i < len(snippets) else ""
        t = _strip_tags(title)
        if t and url.startswith("http"):
            out.append(_make_result(t, url, snip))
    _slog(f"Bing ✓ {len(out)} kết quả")
    return out

async def _search_google(query: str, max_results: int = 8) -> list:
    """Nguồn 'Google' gồm 2 lớp chạy ĐỒNG THỜI (không tăng độ trễ phản hồi):
      1. Google News RSS song ngữ (vi-VN + en-US) — ƯU TIÊN lên đầu danh sách:
         bài báo từ các nguồn báo chí uy tín, sắp xếp theo thời gian mới nhất →
         đáp ứng "chất lượng và cập nhật nhanh nhất".  Cả hai feed chạy song song
         trong _search_news, kết quả gộp lại và sort mới nhất trước.
      2. DDG + Bing song ngữ (vi-VN và en-US) chạy đồng thời — bổ sung kết quả
         web tổng quát.  Chiến lược: chạy 4 backend cùng lúc (DDG-vi, Bing-vi,
         DDG-en, Bing-en), lấy kết quả của backend nào về trước; nếu tiếng Việt
         có kết quả thì dừng, nếu không thì fallback sang kết quả tiếng Anh.

    Kết quả trả về: tin báo đứng trước, kết quả web theo sau, đã loại trùng URL."""
    _slog(f"_search_google: query={repr(query)}, max={max_results}")

    news_task = asyncio.create_task(_search_news(query, max_results=max_results))

    # ── Web backends: vi-VN + en-US chạy song song ─────────────────
    web_vi: list = []
    web_en: list = []

    async def _fetch_web_vi(client: httpx.AsyncClient) -> list:
        """Race DDG-vi vs Bing-vi; trả về kết quả đầu tiên có data."""
        tasks = {
            asyncio.create_task(_backend_ddg(query, max_results, client)): "DDG-vi",
            asyncio.create_task(_backend_bing(query, max_results, client)): "Bing-vi",
        }
        pending = set(tasks)
        result: list = []
        try:
            while pending and not result:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    name = tasks[t]
                    try:
                        res = t.result()
                    except Exception as e:
                        _slog(f"{name} lỗi: {e!r}")
                        continue
                    if res:
                        _slog(f"{name} thành công → {len(res)} kết quả")
                        result = res
                        break
                    _slog(f"{name} trả về rỗng")
        finally:
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return result

    async def _fetch_web_en(client: httpx.AsyncClient) -> list:
        """Race DDG-en vs Bing-en (tiếng Anh) để bổ sung kết quả quốc tế."""
        en_query = query  # giữ nguyên query; DDG/Bing tự ưu tiên kết quả Anh theo locale
        tasks = {
            asyncio.create_task(
                client.post(
                    "https://html.duckduckgo.com/html/",
                    data={"q": en_query, "kl": "us-en"},
                    headers={"User-Agent": _SEARCH_UA},
                    timeout=8,
                )
            ): "DDG-en",
            asyncio.create_task(
                client.get(
                    "https://www.bing.com/search",
                    params={"q": en_query, "count": str(max_results),
                            "mkt": "en-US", "cc": "US", "setlang": "en"},
                    headers={"User-Agent": _SEARCH_UA, "Accept-Language": "en-US,en;q=0.9"},
                    timeout=8,
                )
            ): "Bing-en",
        }
        pending = set(tasks)
        result: list = []
        try:
            while pending and not result:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    name = tasks[t]
                    try:
                        resp = t.result()
                    except Exception as e:
                        _slog(f"{name} lỗi: {e!r}")
                        continue
                    # Parse inline (không dùng helper nhận client)
                    parsed: list = []
                    if "DDG-en" in name:
                        links    = re.findall(r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>', resp.text, re.DOTALL)
                        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
                        for i, (url, title) in enumerate(links[:max_results]):
                            snip = snippets[i] if i < len(snippets) else ""
                            t2 = _strip_tags(title)
                            if t2:
                                parsed.append(_make_result(t2, _unwrap_ddg_url(url), snip))
                    else:  # Bing-en
                        titles   = re.findall(r'<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
                        snippets = re.findall(r'<p class="b_lineclamp[^"]*"[^>]*>(.*?)</p>', resp.text, re.DOTALL)
                        for i, (url, title) in enumerate(titles[:max_results]):
                            if "bing.com" in url and "/search" in url:
                                continue
                            snip = snippets[i] if i < len(snippets) else ""
                            t2 = _strip_tags(title)
                            if t2 and url.startswith("http"):
                                parsed.append(_make_result(t2, url, snip))
                    if parsed:
                        _slog(f"{name} thành công → {len(parsed)} kết quả")
                        result = parsed
                        break
                    _slog(f"{name} trả về rỗng")
        finally:
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return result

    async with httpx.AsyncClient(follow_redirects=True, timeout=8, headers={"User-Agent": _SEARCH_UA}) as client:
        web_vi, web_en = await asyncio.gather(
            _fetch_web_vi(client),
            _fetch_web_en(client),
            return_exceptions=True,
        )
        if isinstance(web_vi, Exception):
            web_vi = []
        if isinstance(web_en, Exception):
            web_en = []

    _slog(f"Web vi={len(web_vi)} kết quả, en={len(web_en)} kết quả")

    # Google News: chờ thêm tối đa 3s rồi bỏ qua nếu chưa có
    news_results: list = []
    try:
        news_results = await asyncio.wait_for(news_task, timeout=3)
    except Exception as e:
        _slog(f"News (trong _search_google) lỗi/timeout: {e!r}")

    seen_urls: set[str] = set()
    news_chunks: list = []
    for r in news_results[:max_results]:
        url = r.get("url", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        meta = " · ".join(x for x in (r.get("source", ""), r.get("published", "")) if x)
        news_chunks.append(_make_result(r.get("title", ""), url, meta))

    # Gộp web: vi-VN trước, en-US bổ sung (không trùng URL)
    web_chunks: list = []
    for r in web_vi + web_en:
        url = r.get("url", "")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        web_chunks.append(r)

    combined = news_chunks + web_chunks
    if combined:
        _slog(f"_search_google: tổng hợp {len(news_chunks)} báo + {len(web_chunks)} web "
              f"(vi={len(web_vi)}, en={len(web_en)})")
        return combined

    _slog("_search_google: TẤT CẢ backend thất bại")
    return [{"title": "Không tìm thấy kết quả", "url": "", "snippet": ""}]

async def _search_news(query: str, max_results: int = 10) -> list:
    """Tìm bài báo/sự kiện mới qua Google News RSS song ngữ (vi-VN + en-US) —
    cả hai feed chạy ĐỒNG THỜI để tăng số bài và độ phủ. Kết quả được gộp lại,
    loại trùng URL, sắp xếp theo thời gian mới nhất (pubDate).
    Được _search_google gọi song song để làm lớp ưu tiên hàng đầu của nguồn 'Google'."""
    _slog(f"News → query={repr(query)}, max={max_results}")
    import xml.etree.ElementTree as ET
    import email.utils as _eutils

    def _parse_feed(content: bytes, lang_label: str) -> list:
        """Parse một RSS feed, trả về list dict có thêm pub_ts để sort."""
        try:
            root = ET.fromstring(content)
        except Exception as e:
            _slog(f"News parse ({lang_label}) ✗ {e!r}")
            return []
        items = []
        for item in root.findall(".//item"):
            title_raw = (item.findtext("title") or "").strip()
            link      = (item.findtext("link") or "").strip()
            pub       = (item.findtext("pubDate") or "").strip()
            src_el    = item.find("source")
            source    = (src_el.text or "").strip() if src_el is not None and src_el.text else ""
            title = title_raw
            if source and title.endswith(f" - {source}"):
                title = title[: -(len(source) + 3)].strip()
            # Parse timestamp để sort; fallback về 0 nếu lỗi
            pub_ts = 0
            if pub:
                try:
                    pub_ts = _eutils.parsedate_to_datetime(pub).timestamp()
                except Exception:
                    pass
            items.append({
                "title": _strip_tags(title), "url": link,
                "source": source, "published": pub, "pub_ts": pub_ts,
            })
        _slog(f"News ({lang_label}) ✓ {len(items)} kết quả thô")
        return items

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=8,
                                      headers={"User-Agent": _SEARCH_UA}) as client:
            resp_vi, resp_en = await asyncio.gather(
                client.get(
                    "https://news.google.com/rss/search",
                    params={"q": query, "hl": "vi", "gl": "VN", "ceid": "VN:vi"},
                ),
                client.get(
                    "https://news.google.com/rss/search",
                    params={"q": query, "hl": "en", "gl": "US", "ceid": "US:en"},
                ),
                return_exceptions=True,
            )

        items_vi = _parse_feed(resp_vi.content,  "vi") if not isinstance(resp_vi,  Exception) else []
        items_en = _parse_feed(resp_en.content, "en") if not isinstance(resp_en, Exception) else []

        # Gộp, loại trùng URL, sort mới nhất trước
        seen_urls: set[str] = set()
        merged: list = []
        for item in items_vi + items_en:
            url = item.get("url", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            merged.append(item)

        merged.sort(key=lambda x: x.get("pub_ts", 0), reverse=True)

        out = merged[:max_results]
        # Bỏ trường pub_ts trước khi trả về (internal only)
        for r in out:
            r.pop("pub_ts", None)

        _slog(f"News ✓ tổng hợp {len(items_vi)} vi + {len(items_en)} en → {len(out)} kết quả sau lọc")
        return out
    except Exception as e:
        _slog(f"News ✗ {e!r}")
        return []

async def _search_facebook(query: str, max_results: int = 5) -> list:
    """Tìm bài viết/fanpage Facebook công khai qua Google (site:facebook.com)."""
    _slog(f"Facebook → query={repr(query)}, max={max_results}")
    try:
        results = await _search_google(f"site:facebook.com {query}", max_results + 4)
    except Exception as e:
        return [{"title": "Lỗi tìm kiếm Facebook", "url": "", "snippet": str(e)[:120]}]

    out, seen = [], set()
    skip = ("/login", "/sharer", "/dialog", "/help", "/policies",
            "/ads/", "/legal", "/recover", "l.facebook.com")
    for r in results:
        url = r.get("url", "")
        if "facebook.com" not in url or any(s in url for s in skip):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"title": r.get("title", ""), "url": url, "snippet": r.get("snippet", "")})
        if len(out) >= max_results:
            break
    _slog(f"Facebook ✓ {len(out)} kết quả")
    return out or [{"title": "Không tìm thấy kết quả Facebook", "url": "", "snippet": ""}]

async def _search_youtube(query: str, max_results: int = 10, order: str = "relevance") -> list:
    """Tìm video YouTube qua trang search (scrape ytInitialData) — trả về title + url + channel.
    max_results là kích thước POOL lấy về để AI chọn lọc, không phải số video sẽ hiển thị.

    order:
      - "relevance" (mặc định)  → liên quan nhất
      - "date"                  → mới nhất (upload date)
      - "views"                 → xem nhiều nhất (view count)
      - "rating"                → đánh giá cao nhất
    """
    _slog(f"YouTube → query={repr(query)}, max={max_results}, order={order}")
    import json as _json

    _SP_MAP = {
        "date":   "CAI%3D",
        "views":  "CAM%3D",
        "rating": "CAE%3D",
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=12,
                                      headers={"User-Agent": _SEARCH_UA,
                                               "Accept-Language": "en-US,en;q=0.9"}) as client:
            params = {"search_query": query}
            sp = _SP_MAP.get(order)
            if sp:
                params["sp"] = sp
            resp = await client.get("https://www.youtube.com/results", params=params)
        _slog(f"YouTube ← status={resp.status_code}, len={len(resp.text)}")
        m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});", resp.text, re.DOTALL)
        if not m:
            m = re.search(r"ytInitialData\"\]\s*=\s*(\{.*?\});", resp.text, re.DOTALL)
        if not m:
            _slog("YouTube ✗ không tìm thấy ytInitialData")
            return []
        data = _json.loads(m.group(1))

        out = []
        def _walk(node):
            if len(out) >= max_results:
                return
            if isinstance(node, dict):
                if "videoRenderer" in node:
                    vr = node["videoRenderer"]
                    vid = vr.get("videoId", "")
                    title_runs = vr.get("title", {}).get("runs", [])
                    title = "".join(r.get("text", "") for r in title_runs) or vr.get("title", {}).get("simpleText", "")
                    channel = ""
                    owner = vr.get("ownerText", {}).get("runs", [])
                    if owner:
                        channel = owner[0].get("text", "")
                    duration = vr.get("lengthText", {}).get("simpleText", "")
                    views = ""
                    for vline in vr.get("shortViewCountText", {}).get("runs", []) or []:
                        views += vline.get("text", "")
                    if not views:
                        views = vr.get("shortViewCountText", {}).get("simpleText", "")
                    published = vr.get("publishedTimeText", {}).get("simpleText", "")
                    desc = ""
                    for dline in vr.get("detailedMetadataSnippets", []) or []:
                        for r2 in dline.get("snippetText", {}).get("runs", []) or []:
                            desc += r2.get("text", "")
                        if desc:
                            break
                    if vid and title:
                        out.append({
                            "title": _strip_tags(title),
                            "url": f"https://www.youtube.com/watch?v={vid}",
                            "channel": channel,
                            "duration": duration,
                            "views": views,
                            "published": published,
                            "desc": _strip_tags(desc)[:150],
                        })
                    return
                for v in node.values():
                    _walk(v)
                    if len(out) >= max_results:
                        return
            elif isinstance(node, list):
                for v in node:
                    _walk(v)
                    if len(out) >= max_results:
                        return

        _walk(data)
        _slog(f"YouTube ✓ {len(out)} kết quả")
        return out
    except Exception as e:
        _slog(f"YouTube ✗ {e!r}")
        return []


async def _search_youtube_trending(max_results: int = 15) -> list:
    """Lấy danh sách video trending YouTube Việt Nam (scrape ytInitialData từ /feed/trending)."""
    _slog(f"YouTube Trending → max={max_results}")
    import json as _json
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=12,
                                      headers={"User-Agent": _SEARCH_UA,
                                               "Accept-Language": "vi-VN,vi;q=0.9"}) as client:
            resp = await client.get(
                "https://www.youtube.com/feed/trending",
                params={"gl": "VN"},
            )
        _slog(f"YouTube Trending ← status={resp.status_code}, len={len(resp.text)}")
        m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});", resp.text, re.DOTALL)
        if not m:
            m = re.search(r"ytInitialData\"\]\s*=\s*(\{.*?\});", resp.text, re.DOTALL)
        if not m:
            _slog("YouTube Trending ✗ không tìm thấy ytInitialData")
            return []
        data = _json.loads(m.group(1))

        out = []
        def _walk(node):
            if len(out) >= max_results:
                return
            if isinstance(node, dict):
                if "videoRenderer" in node:
                    vr = node["videoRenderer"]
                    vid = vr.get("videoId", "")
                    title_runs = vr.get("title", {}).get("runs", [])
                    title = "".join(r.get("text", "") for r in title_runs) or vr.get("title", {}).get("simpleText", "")
                    channel = ""
                    owner = vr.get("ownerText", {}).get("runs", [])
                    if owner:
                        channel = owner[0].get("text", "")
                    duration = vr.get("lengthText", {}).get("simpleText", "")
                    views = ""
                    for vline in vr.get("shortViewCountText", {}).get("runs", []) or []:
                        views += vline.get("text", "")
                    if not views:
                        views = vr.get("shortViewCountText", {}).get("simpleText", "")
                    published = vr.get("publishedTimeText", {}).get("simpleText", "")
                    if vid and title:
                        out.append({
                            "title": _strip_tags(title),
                            "url": f"https://www.youtube.com/watch?v={vid}",
                            "channel": channel,
                            "duration": duration,
                            "views": views,
                            "published": published,
                        })
                    return
                for v in node.values():
                    _walk(v)
                    if len(out) >= max_results:
                        return
            elif isinstance(node, list):
                for v in node:
                    _walk(v)
                    if len(out) >= max_results:
                        return

        _walk(data)
        _slog(f"YouTube Trending ✓ {len(out)} kết quả")
        return out
    except Exception as e:
        _slog(f"YouTube Trending ✗ {e!r}")
        return []


async def _ai_expand_youtube_query(user_text: str) -> list[str]:
    """Dùng AI để mở rộng/dịch câu tìm kiếm YouTube của người dùng thành nhiều
    truy vấn tìm kiếm khác nhau (đa ngôn ngữ + diễn giải ý định thật).

    Ví dụ: "tìm bài hát của miku" → AI hiểu đây là nhân vật Vocaloid Hatsune Miku,
    nên trả về các query như "Hatsune Miku song", "初音ミク 曲", "Hatsune Miku Vocaloid"...
    chứ không chỉ dịch nguyên văn "miku songs" sang tiếng Việt.

    Trả về list các query (tối đa 4), luôn có ít nhất query gốc nếu AI lỗi/không khả dụng."""
    fallback = [user_text]
    if not GEMINI_AVAILABLE or not _get_all_ai_keys():
        return fallback

    prompt = (
        f'Người dùng muốn tìm video trên YouTube với yêu cầu: "{user_text}"\n\n'
        "Hãy phân tích Ý ĐỊNH THẬT của người dùng. Đặc biệt: nếu họ nhắc tên một NHÂN VẬT giọng hát "
        "tổng hợp/Vocaloid/UTAU/CeVIO (ví dụ: Hatsune Miku, Kasane Teto, Kagamine Rin/Len, Megurine Luka, "
        "GUMI, IA, Hatsune Miku V4X, v.v.), thì hiểu rằng có HÀNG NGÀN bài hát do RẤT NHIỀU nhà sản xuất/kênh "
        "(producer/Vocalo-P) khác nhau làm ra cho nhân vật đó (ví dụ DECO*27, Kikuo, Reol, AnythingBecomeMoe, "
        "Mafumafu, Giga, v.v.) — KHÔNG chỉ giới hạn ở 1 kênh 'chính thức' duy nhất. Vì vậy hãy tạo các query "
        "đa dạng để tìm ra nhiều BÀI HÁT/MV từ nhiều nhà sản xuất/kênh khác nhau dùng nhân vật đó "
        "(ví dụ: '初音ミク オリジナル曲', 'Kasane Teto song', 'Vocaloid cover [tên bài hoặc chủ đề nếu có]'), "
        "thay vì chỉ tìm tên nhân vật một cách chung chung.\n\n"
        "Nếu họ nhắc tên một ca sĩ/chủ đề/sự kiện khác (không phải Vocaloid), áp dụng tương tự: "
        "hiểu đúng đó là ai/là gì để tìm đúng nội dung.\n\n"
        "Sau đó tạo ra TỐI ĐA 4 câu truy vấn tìm kiếm YouTube khác nhau, bằng các ngôn ngữ phù hợp "
        "nhất với nội dung đó (ví dụ: nhạc Nhật → có query tiếng Nhật; nhạc Hàn → có query tiếng Hàn; "
        "tin tức/giải trí chung → có thể kèm tiếng Anh và tiếng Việt) để tăng khả năng tìm ra kết quả "
        "chất lượng, đa dạng nguồn/kênh.\n\n"
        "CHỈ trả lời bằng JSON thuần, dạng: [\"query 1\", \"query 2\", \"query 3\", \"query 4\"]\n"
        "Không thêm bất kỳ giải thích, markdown, hay text nào khác ngoài JSON array."
    )

    model_name = CONFIG.get("gemini_model", "gemini-3.1-flash-lite")
    all_keys   = _get_all_ai_keys()
    import json as _json
    for i, key in enumerate(all_keys):
        try:
            client   = genai.Client(api_key=key)
            response = await asyncio.to_thread(
                client.models.generate_content, model=model_name, contents=prompt,
            )
            raw = response.text.strip()
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
            queries = _json.loads(raw)
            if isinstance(queries, list) and queries:
                queries = [str(q).strip() for q in queries if str(q).strip()][:4]
                if queries:
                    _slog(f"_ai_expand_youtube_query: {user_text!r} → {queries}")
                    return queries
        except Exception as e:
            if any(x in str(e).lower() for x in ["quota", "rate", "429", "exhausted", "resource_exhausted"]):
                continue
            _slog(f"_ai_expand_youtube_query key #{i+1} lỗi: {e!r}")
            break
    return fallback



_YT_LINK_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([A-Za-z0-9_-]+)"
)


async def _fix_youtube_links(reply: str, yt_pool: list) -> str:
    """Hậu kiểm NGHIÊM NGẶT: mọi link YouTube trong reply CHỈ được giữ nếu khớp (hoặc
    gần khớp, lệch đúng 1 ký tự — lỗi gõ nhầm) với một video THỰC SỰ CÓ TRONG POOL
    đã search được ở lượt này.

    Không có ngoại lệ "video thật ngoài pool" — vì nếu link không nằm trong pool,
    đó là dấu hiệu AI đang dùng trí nhớ/huấn luyện cũ (hallucination nguồn), dù link
    đó có tồn tại thật hay không, nên vẫn phải loại bỏ để đảm bảo chỉ giới thiệu
    kết quả từ lần search hiện tại.

    LƯU Ý QUAN TRỌNG VỀ MARKDOWN: link hợp lệ được BỌC thành `[tiêu đề video](url)`
    (markdown link thật) thay vì để URL trần trong câu. Lý do: nơi gọi cuối cùng
    (safe_reply) tự động chạy to_markdown_v2() trên toàn bộ reply — nếu để URL
    trần, to_markdown_v2 sẽ escape MỌI ký tự đặc biệt trong URL (`.` `_` `=`...)
    vì không có cách nào phân biệt "URL cần giữ nguyên" với "text thường chứa
    ký tự đặc biệt", làm link vỡ/không bấm được. Bọc thành [text](url) thì
    to_markdown_v2 nhận diện đúng là markdown link, chỉ escape `)` và `\\` bên
    trong URL (đúng chuẩn MarkdownV2), giữ nguyên link bấm được."""
    if not reply:
        return reply

    url_to_title = {v["url"]: v.get("title", "") for v in yt_pool if v.get("url")}
    valid_urls   = set(url_to_title)
    valid_ids    = {m.group(1) for u in valid_urls
                     for m in [re.search(r"v=([A-Za-z0-9_-]{11})", u)] if m}

    def _hamming_close(a: str, b: str, max_diff: int = 1) -> bool:
        """True nếu a, b cùng độ dài 11 và khác nhau tối đa max_diff ký tự."""
        if len(a) != 11 or len(b) != 11:
            return False
        diff = sum(1 for x, y in zip(a, b) if x != y)
        return diff <= max_diff

    def _as_link(url: str) -> str:
        """Bọc `url` thành markdown link [tiêu đề](url) — escape title thô
        (sẽ được to_markdown_v2 escape đúng cách ở vòng ngoài cùng vì nó vẫn
        nằm trong cú pháp [..](..) nhận diện được), cắt bớt nếu quá dài."""
        title = (url_to_title.get(url) or "").strip()
        if not title:
            title = url  # fallback: không có tiêu đề thì hiện chính URL làm chữ
        if len(title) > 100:
            title = title[:97] + "..."
        # markdown link text không được chứa "[" "]" thô — thay bằng dấu ngoặc thường
        title = title.replace("[", "(").replace("]", ")")
        return f"[{title}]({url})"

    matches = list(_YT_LINK_RE.finditer(reply))
    replacements: dict[str, str] = {}  # full_url -> giá trị thay thế ("" để xoá)

    for m in matches:
        full_url = m.group(0)
        vid      = m.group(1)
        if full_url in replacements:
            continue

        if full_url in valid_urls:
            replacements[full_url] = _as_link(full_url)
            continue

        # Khớp gần đúng với 1 video trong pool (lệch ≤1 ký tự, ví dụ AI gõ nhầm 1 ký tự)
        fixed_id = None
        for good_id in valid_ids:
            if _hamming_close(vid, good_id, max_diff=1):
                fixed_id = good_id
                break
            # Trường hợp AI thiếu/dư 1 ký tự (10 hoặc 12 ký tự) nhưng phần còn lại khớp
            if len(vid) == 10 and len(good_id) == 11 and good_id[:10] == vid:
                fixed_id = good_id
                break
            if len(vid) == 12 and len(good_id) == 11 and vid[:11] == good_id:
                fixed_id = good_id
                break

        if fixed_id:
            fixed = f"https://www.youtube.com/watch?v={fixed_id}"
            _slog(f"_fix_youtube_links: sửa link gần đúng {full_url!r} → {fixed!r}")
            replacements[full_url] = _as_link(fixed)
            continue

        # Không khớp pool ở mức nào → coi là bịa/nhớ nhầm từ training data, xoá
        _slog(f"_fix_youtube_links: link không khớp pool, xoá: {full_url!r}")
        replacements[full_url] = ""

    fixed_reply = _YT_LINK_RE.sub(lambda m: replacements.get(m.group(0), ""), reply)
    # Dọn khoảng trắng dư ra sau khi xoá link rỗng
    fixed_reply = re.sub(r"[ \t]+\n", "\n", fixed_reply)
    fixed_reply = re.sub(r"\n{3,}", "\n\n", fixed_reply)
    return fixed_reply.strip()
