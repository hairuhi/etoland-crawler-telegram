import os
import re
import json
import time
import pathlib
from io import BytesIO
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ========= Telegram =========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ========= Target (Yakhu only) =========
HGALL_URL = "https://www.etoland.co.kr/bbs/hgall.php?bo_table=etohumor07&sca=%BE%E0%C8%C4"
TARGET_BOARD = "etohumor07"

# ========= State / Debug =========
SEEN_FILE = os.getenv("SEEN_SET_FILE", "state/seen_ids.txt")
ENABLE_HEARTBEAT = os.getenv("ENABLE_HEARTBEAT", "0").strip() == "1"
HEARTBEAT_TEXT = os.getenv("HEARTBEAT_TEXT", "🧪 Heartbeat: bot alive.")
FORCE_SEND_LATEST = os.getenv("FORCE_SEND_LATEST", "0").strip() == "1"
RESET_SEEN = os.getenv("RESET_SEEN", "0").strip() == "1"

# ========= Behavior toggles =========
DOWNLOAD_AND_UPLOAD = os.getenv("DOWNLOAD_AND_UPLOAD", "0").strip() == "1"
TRACE_IMAGE_DEBUG = os.getenv("TRACE_IMAGE_DEBUG", "0").strip() == "1"

# ========= Exclude patterns =========
EXCLUDE_IMAGE_SUBSTRINGS = [
    "link.php?",
    "/logo/",
    "/banner/",
    "/ads/",
    "/noimage",
    "/favicon",
    "/thumb/",
    "/placeholder/",
    "/img/icon_link.gif",  # ← 에토랜드 L 아이콘 직접 차단
    "icon_link.gif"
]
_extra = os.getenv("EXCLUDE_IMAGE_SUBSTRINGS", "").strip()
if _extra:
    EXCLUDE_IMAGE_SUBSTRINGS += [s.strip() for s in _extra.split(",") if s.strip()]

# ========= HTTP =========
SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (compatible; EtolandYakhuOnly/1.6)",
        "Accept-Language": "ko,ko-KR;q=0.9,en;q=0.8",
        "Referer": "https://www.etoland.co.kr/",
        "Connection": "close",
    }
)
TIMEOUT = 20


# --- Utils ---
def ensure_state_dir():
    pathlib.Path("state").mkdir(parents=True, exist_ok=True)


def load_seen() -> set:
    ensure_state_dir()
    if RESET_SEEN:
        print("[debug] RESET_SEEN=1 → ignoring previous seen set this run")
        return set()
    s = set()
    p = pathlib.Path(SEEN_FILE)
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        s.add(line.strip())
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


# --- Exclude Logic ---
PLACEHOLDER_ICON_NAMES = {"icon_link.gif"}

def is_placeholder_icon(url: str) -> bool:
    """정확히 icon_link.gif (L 아이콘) 차단"""
    try:
        path = urlparse(url).path.lower()
        filename = path.rsplit("/", 1)[-1]
        return (filename in PLACEHOLDER_ICON_NAMES) or ("/img/icon_link.gif" in path)
    except Exception:
        return False


def is_excluded_image(url: str) -> bool:
    low = url.lower()
    return any(hint.lower() in low for hint in EXCLUDE_IMAGE_SUBSTRINGS)


# --- Telegram API ---
def tg_post(method: str, data: dict, files=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    r = requests.post(url, data=data, files=files, timeout=60)
    try:
        j = r.json()
    except Exception:
        j = {"non_json_body": r.text[:500]}
    print(f"[tg] {method} status={r.status_code} ok={j.get('ok')} desc={j.get('description')}")
    return r, j


def tg_send_text(text: str):
    return tg_post(
        "sendMessage",
        {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False},
    )


def build_caption(title: str, url: str, summary: str) -> str:
    caption = f"📌 <b>{title}</b>"
    if summary:
        caption += f"\n{summary}"
    caption += f"\n{url}"
    if len(caption) > 900:
        caption = caption[:897] + "…"
    return caption


# --- Parser Helpers ---
def text_summary_from_html(soup: BeautifulSoup, max_chars=280) -> str:
    sel = ["#bo_v_con", ".bo_v_con", "div.view_content", ".viewContent", "#view_content", "article"]
    container = next((soup.select_one(s) for s in sel if soup.select_one(s)), soup)
    for tag in container(["script", "style", "noscript"]):
        tag.extract()
    text = re.sub(r"\s+", " ", container.get_text(" ", strip=True)).strip()
    return text[: max_chars - 1] + "…" if len(text) > max_chars else text


def fetch_hgall_yakhu_list() -> list[dict]:
    """약후 리스트에서 wr_id 링크 느슨하게 수집"""
    r = SESSION.get(HGALL_URL, timeout=TIMEOUT)
    html = get_encoding_safe_text(r)
    soup = BeautifulSoup(html, "html.parser")

    posts = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or "wr_id=" not in href:
            continue

        m = re.search(r"wr_id=(\d+)", href)
        if not m:
            continue
        wr_id = int(m.group(1))

        bo_table = TARGET_BOARD
        bm = re.search(r"bo_table=([a-z0-9_]+)", href, re.I)
        if bm:
            bo_table = bm.group(1).lower()

        if bo_table != TARGET_BOARD:
            continue

        title = a.get_text(strip=True)
        if not title:
            continue

        full_url = absolutize(HGALL_URL, href)
        posts.append({"bo_table": bo_table, "wr_id": wr_id, "title": title, "url": full_url})

    posts = sorted({(p["bo_table"], p["wr_id"]): p for p in posts}.values(),
                   key=lambda x: x["wr_id"], reverse=True)

    print(f"[debug] 약후 리스트 수집 완료: {len(posts)}개")
    return posts


# --- Content Fetcher ---
def fetch_content_media_and_summary(post_url: str) -> dict:
    r = SESSION.get(post_url, timeout=TIMEOUT)
    html = get_encoding_safe_text(r)
    soup = BeautifulSoup(html, "html.parser")
    summary = text_summary_from_html(soup)

    candidates = ["#bo_v_con", ".bo_v_con", "div.view_content", ".viewContent", "#view_content", "article"]
    container = next((soup.select_one(s) for s in candidates if soup.select_one(s)), soup)

    # 1️⃣ <img> 수집
    all_imgs = []
    for img in container.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original") or img.get("data-echo")
        if src:
            all_imgs.append(absolutize(post_url, src))

    if TRACE_IMAGE_DEBUG:
        print("[trace] before-filter:", all_imgs[:10])

    images = []
    for u in all_imgs:
        if is_placeholder_icon(u):
            continue  # L 아이콘 차단
        if not is_excluded_image(u):
            images.append(u)

    # 2️⃣ <a href="*.jpg|png|gif|webp"> 도 이미지로 인식
    if not images:
        for a in container.find_all("a", href=True):
            href = a["href"].strip()
            if not href:
                continue
            full = absolutize(post_url, href)
            if re.search(r"\.(jpg|jpeg|png|gif|webp)(?:\?|$)", full, re.I):
                if is_placeholder_icon(full):
                    continue
                if not is_excluded_image(full):
                    images.append(full)

    if TRACE_IMAGE_DEBUG:
        print("[trace] after-filter :", images[:10])
        try:
            preview = "\n".join(images[:10]) or "(no images)"
            tg_send_text("🔍 image candidates:\n" + preview)
        except Exception as e:
            print("[trace] send preview failed:", e)

    # 3️⃣ 비디오, iframe
    video_exts = (".mp4", ".mov", ".webm", ".mkv", ".m4v")
    videos = []
    for v in container.find_all(["video", "source"]):
        src = v.get("src")
        if src and any(src.lower().endswith(ext) for ext in video_exts):
            videos.append(absolutize(post_url, src))
    iframes = [absolutize(post_url, f.get("src")) for f in container.find_all("iframe") if f.get("src")]

    # 제목
    title = None
    ogt = soup.find("meta", property="og:title")
    if ogt and ogt.get("content"):
        title = ogt.get("content").strip()
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()

    return {"images": images, "videos": videos, "iframes": iframes, "summary": summary, "title_override": title}


# --- Senders ---
def download_bytes(url: str, referer: str) -> bytes | None:
    try:
        headers = {"Referer": referer}
        resp = SESSION.get(url, headers=headers, timeout=TIMEOUT)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception as e:
        print(f"[warn] download failed: {url} err={e}")
    return None


def send_photo_url_or_file(url: str, caption: str | None, referer: str):
    if DOWNLOAD_AND_UPLOAD:
        data = download_bytes(url, referer)
        if data:
            files = {"photo": ("image.jpg", BytesIO(data))}
            return tg_post("sendPhoto", {"chat_id": TELEGRAM_CHAT_ID, "caption": caption or "", "parse_mode": "HTML"}, files)
    return tg_post("sendPhoto", {"chat_id": TELEGRAM_CHAT_ID, "photo": url, "caption": caption or "", "parse_mode": "HTML"})


def send_video_url_or_file(url: str, caption: str | None, referer: str):
    if DOWNLOAD_AND_UPLOAD:
        data = download_bytes(url, referer)
        if data:
            files = {"video": ("video.mp4", BytesIO(data))}
            return tg_post("sendVideo", {"chat_id": TELEGRAM_CHAT_ID, "caption": caption or "", "parse_mode": "HTML"}, files)
    return tg_post("sendVideo", {"chat_id": TELEGRAM_CHAT_ID, "video": url, "caption": caption or "", "parse_mode": "HTML"})


# --- Main ---
def process():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID is required")

    if ENABLE_HEARTBEAT:
        tg_send_text(HEARTBEAT_TEXT)

    posts = fetch_hgall_yakhu_list()
    print("[debug] fetched posts (top5):", [(p["wr_id"], p["title"][:20]) for p in posts[:5]])
    posts.sort(key=lambda x: x["wr_id"])  # oldest first

    seen = load_seen()
    to_send = []
    for p in posts:
        key = f"etoland:{p['bo_table']}:{p['wr_id']}"
        if key not in seen:
            p["_seen_key"] = key
            to_send.append(p)

    if FORCE_SEND_LATEST and not to_send and posts:
        latest = sorted(posts, key=lambda x: x["wr_id"], reverse=True)[0]
        latest["_seen_key"] = f"etoland:{latest['bo_table']}:{latest['wr_id']}"
        to_send = [latest]
        print("[debug] FORCE_SEND_LATEST=1 → most recent 1 post forced to send")

    if not to_send:
        print("[info] no new posts")
        return

    sent_keys = []
    for p in to_send:
        title, url = p["title"], p["url"]
        media = fetch_content_media_and_summary(url)
        if media.get("title_override"):
            title = media["title_override"]
        images, videos, iframes, summary = media["images"], media["videos"], media["iframes"], media["summary"]

        caption = build_caption(title, url, summary)
        tg_send_text(caption)
        time.sleep(1)

        print(f"[debug] media counts wr_id={p['wr_id']}: img={len(images)} vid={len(videos)} ifr={len(iframes)}")

        for img in images:
            send_photo_url_or_file(img, None, url)
            time.sleep(1)
        for vid in videos:
            send_video_url_or_file(vid, None, url)
            time.sleep(1)
        if iframes:
            tg_send_text("🎥 임베드:\n" + "\n".join(iframes[:5]))
            time.sleep(1)

        sent_keys.append(p["_seen_key"])

    append_seen(sent_keys)
    print(f"[info] appended {len(sent_keys)} keys")


if __name__ == "__main__":
    process()
