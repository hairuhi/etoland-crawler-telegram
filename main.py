import os
import re
import json
import time
import pathlib
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

HGALL_URL = "https://www.etoland.co.kr/bbs/hgall.php?bo_table=etohumor07&sca=%BE%E0%C8%C4"
TARGET_BOARD = "etohumor07"

SEEN_FILE = os.getenv("SEEN_SET_FILE", "state/seen_ids.txt")
ENABLE_HEARTBEAT = os.getenv("ENABLE_HEARTBEAT", "0").strip() == "1"
HEARTBEAT_TEXT   = os.getenv("HEARTBEAT_TEXT", "🧪 Heartbeat: bot alive.")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; EtolandYakhuOnly/1.1; +https://github.com/your/repo)",
    "Accept-Language": "ko,ko-KR;q=0.9,en;q=0.8",
    "Referer": "https://www.etoland.co.kr/",
    "Connection": "close",
})
TIMEOUT = 15

def ensure_state_dir():
    pathlib.Path("state").mkdir(parents=True, exist_ok=True)

def load_seen() -> set:
    ensure_state_dir()
    s = set()
    p = pathlib.Path(SEEN_FILE)
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        s.add(line)
        except Exception:
            pass
    return s

def append_seen(keys: list[str]):
    if not keys:
        return
    ensure_state_dir()
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        for k in keys:
            f.write(k + "\n")

def get_encoding_safe_text(resp: requests.Response) -> str:
    if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ansi_x3.4-1968"):
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text

def absolutize(base: str, url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base, url)

LINK_RE = re.compile(
    r"(?:^|/)(?:board\.php|plugin/mobile/board\.php)\?[^"'>]*\bbo_table=([a-z0-9_]+)\b[^"'>]*\bwr_id=(\d+)\b",
    re.I,
)

def extract_bo_and_id(href: str):
    if not href:
        return None, None
    m = LINK_RE.search(href)
    if m:
        bo = m.group(1).lower()
        try:
            wr = int(m.group(2))
        except Exception:
            wr = None
        return bo, wr
    try:
        u = urlparse(href)
        if not (u.path.endswith("board.php") or u.path.endswith("plugin/mobile/board.php")):
            return None, None
        q = parse_qs(u.query)
        bo = (q.get("bo_table", [""])[0] or "").lower()
        wr = int(q.get("wr_id", ["0"])[0])
        return bo or None, wr or None
    except Exception:
        return None, None

def text_summary_from_html(soup: BeautifulSoup, max_chars: int = 280) -> str:
    candidates = ["#bo_v_con", ".bo_v_con", "div.view_content", ".viewContent", "#view_content", "article"]
    container = None
    for sel in candidates:
        node = soup.select_one(sel)
        if node:
            container = node
            break
    if container is None:
        container = soup
    for tag in container(["script", "style", "noscript"]):
        tag.extract()
    text = container.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:max_chars - 1] + "…") if (text and len(text) > max_chars) else (text or "")

def fetch_content_media_and_summary(post_url: str) -> dict:
    r = SESSION.get(post_url, timeout=TIMEOUT)
    html = get_encoding_safe_text(r)
    soup = BeautifulSoup(html, "html.parser")

    summary = text_summary_from_html(soup, max_chars=280)

    candidates = ["#bo_v_con", ".bo_v_con", "div.view_content", ".viewContent", "#view_content", "article"]
    container = None
    for sel in candidates:
        node = soup.select_one(sel)
        if node:
            container = node
            break
    if container is None:
        container = soup

    images = []
    for img in container.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original") or img.get("data-echo")
        if not src:
            continue
        images.append(absolutize(post_url, src))

    video_exts = (".mp4", ".mov", ".webm", ".mkv", ".m4v")
    videos = []
    for v in container.find_all(["video", "source"]):
        src = v.get("src")
        if not src:
            continue
        src = absolutize(post_url, src)
        if any(src.lower().endswith(ext) for ext in video_exts):
            videos.append(src)

    iframes = []
    for f in container.find_all("iframe"):
        src = f.get("src")
        if src:
            iframes.append(absolutize(post_url, src))

    images = list(dict.fromkeys(images))
    videos = list(dict.fromkeys(videos))
    iframes = list(dict.fromkeys(iframes))

    title = None
    ogt = soup.find("meta", property="og:title")
    if ogt and ogt.get("content"):
        title = ogt.get("content").strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()

    return {"images": images, "videos": videos, "iframes": iframes, "summary": summary, "title_override": title}

def tg_post(method: str, data: dict):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    r = requests.post(url, data=data, timeout=20)
    try:
        j = r.json()
    except Exception:
        j = {"non_json_body": r.text[:500]}
    print(f"[tg] {method} status={r.status_code} ok={j.get('ok')} desc={j.get('description')}")
    return r, j

def tg_send_text(text: str):
    return tg_post("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    })

def tg_send_media_group(media_items: list[dict]):
    return tg_post("sendMediaGroup", {
        "chat_id": TELEGRAM_CHAT_ID,
        "media": json.dumps(media_items, ensure_ascii=False)
    })

def build_caption(title: str, url: str, summary: str, batch_idx: int | None, total_batches: int | None) -> str:
    prefix = f"📌 <b>{title}</b>"
    if batch_idx is not None and total_batches is not None and total_batches > 1:
        prefix += f"  ({batch_idx}/{total_batches})"
    body = f"\n{summary}" if summary else ""
    suffix = f"\n{url}"
    caption = f"{prefix}{body}{suffix}"
    if len(caption) > 900:
        caption = caption[:897] + "…"
    return caption

def fetch_hgall_yakhu_list() -> list[dict]:
    r = SESSION.get(HGALL_URL, timeout=TIMEOUT)
    html = get_encoding_safe_text(r)
    soup = BeautifulSoup(html, "html.parser")

    containers = soup.select(".gall_list, .list, #list, .tbl_wrap, .tbl_head01")
    scope = containers[0] if containers else soup

    posts = {}
    for a in scope.find_all("a", href=True):
        href = a["href"].strip()
        if "board.php" not in href:
            continue
        bo, wr = extract_bo_and_id(href)
        if bo != TARGET_BOARD or not wr:
            continue
        title = a.get_text(strip=True) or f"[{bo}] 글번호 {wr}"
        link = absolutize(HGALL_URL, href)
        key = (bo, wr)
        if key not in posts or (title and len(title) > len(posts[key]["title"])):
            posts[key] = {"bo_table": bo, "wr_id": wr, "title": title, "url": link}

    res = sorted(posts.values(), key=lambda x: x["wr_id"], reverse=True)
    print(f"[debug] hgall(약후 strict) list fetched: {len(res)} items")
    return res

def process():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID is required")

    if ENABLE_HEARTBEAT:
        tg_send_text(HEARTBEAT_TEXT)

    posts = fetch_hgall_yakhu_list()
    posts.sort(key=lambda x: x["wr_id"])

    seen = load_seen()
    to_send = []
    for p in posts:
        key = f"etoland:{p['bo_table']}:{p['wr_id']}"
        if key not in seen:
            p["_seen_key"] = key
            to_send.append(p)

    if not to_send:
        print("[info] no new posts")
        return

    sent_keys = []
    for p in to_send:
        title = p["title"]
        url   = p["url"]

        media = fetch_content_media_and_summary(url)
        if media.get("title_override"):
            title = media["title_override"]

        images = media["images"]
        videos = media["videos"]
        iframes = media["iframes"]
        summary = media["summary"]

        media_urls = images + videos
        MAX_ITEMS = 10

        if not media_urls:
            caption = build_caption(title, url, summary, None, None)
            tg_send_text(caption)
            sent_keys.append(p["_seen_key"])
            time.sleep(1)
            continue

        total = len(media_urls)
        total_batches = (total + MAX_ITEMS - 1) // MAX_ITEMS

        for batch_idx in range(total_batches):
            start = batch_idx * MAX_ITEMS
            end = min(start + MAX_ITEMS, total)
            chunk = media_urls[start:end]

            media_items = []
            for i, murl in enumerate(chunk):
                typ = "video" if any(murl.lower().endswith(ext) for ext in (".mp4", ".mov", ".webm", ".mkv", ".m4v")) else "photo"
                item = {"type": typ, "media": murl}
                if batch_idx == 0 and i == 0:
                    item["caption"] = build_caption(title, url, summary, batch_idx + 1, total_batches)
                    item["parse_mode"] = "HTML"
                elif i == 0 and total_batches > 1:
                    item["caption"] = f"({batch_idx + 1}/{total_batches}) 계속"
                media_items.append(item)

            r, j = tg_send_media_group(media_items)
            if not j.get("ok"):
                tg_send_text(build_caption(title, url, summary, batch_idx + 1, total_batches))
            time.sleep(1)

        if iframes:
            tg_send_text("🎬 임베드 동영상 링크:
" + "
".join(iframes[:5]))

        sent_keys.append(p["_seen_key"])
        time.sleep(1)

    append_seen(sent_keys)
    print(f"[info] appended {len(sent_keys)} keys")

if __name__ == "__main__":
    process()
