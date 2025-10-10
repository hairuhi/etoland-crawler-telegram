import os
import re
import json
import time
import pathlib
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

# ========= Telegram =========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ========= Target (STRICT to Yakhu) =========
HGALL_URL = "https://www.etoland.co.kr/bbs/hgall.php?bo_table=etohumor07&sca=%BE%E0%C8%C4"
TARGET_BOARD = "etohumor07"

# ========= State / Debug =========
SEEN_FILE = os.getenv("SEEN_SET_FILE", "state/seen_ids.txt")  # etoland:etohumor07:wr_id
ENABLE_HEARTBEAT = os.getenv("ENABLE_HEARTBEAT", "0").strip() == "1"
HEARTBEAT_TEXT = os.getenv("HEARTBEAT_TEXT", "🧪 Heartbeat: bot alive.")

# Debug helpers
FORCE_SEND_LATEST = os.getenv("FORCE_SEND_LATEST", "0").strip() == "1"
RESET_SEEN = os.getenv("RESET_SEEN", "0").strip() == "1"

# ========= HTTP =========
SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (compatible; EtolandYakhuOnly/1.4; +https://github.com/your/repo)",
        "Accept-Language": "ko,ko-KR;q=0.9,en;q=0.8",
        "Referer": "https://www.etoland.co.kr/",
        "Connection": "close",
    }
)
TIMEOUT = 15


def ensure_state_dir() -> None:
    pathlib.Path("state").mkdir(parents=True, exist_ok=True)


def load_seen() -> set:
    ensure_state_dir()
    if RESET_SEEN:
        print("[debug] RESET_SEEN=1 → seen set ignored this run")
        return set()
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


def append_seen(keys: list[str]) -> None:
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


def text_summary_from_html(soup: BeautifulSoup, max_chars: int = 280) -> str:
    # 본문 후보 컨테이너
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
    if not text:
        return ""
    return text[: max_chars - 1] + "…" if len(text) > max_chars else text


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

    # images
    images = []
    for img in container.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original") or img.get("data-echo")
        if not src:
            continue
        images.append(absolutize(post_url, src))

    # 첫 썸네일(사이트 placeholder) 제거: 2장 이상일 때 1장 버림
    if len(images) >= 2:
        images = images[1:]

    # direct videos
    video_exts = (".mp4", ".mov", ".webm", ".mkv", ".m4v")
    videos = []
    for v in container.find_all(["video", "source"]):
        src = v.get("src")
        if not src:
            continue
        src = absolutize(post_url, src)
        if any(src.lower().endswith(ext) for ext in video_exts):
            videos.append(src)

    # iframes (유튜브 등) → 링크로 안내
    iframes = []
    for f in container.find_all("iframe"):
        src = f.get("src")
        if src:
            iframes.append(absolutize(post_url, src))

    # dedup
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
    return tg_post(
        "sendMessage",
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
    )


def tg_send_media_group(media_items: list[dict]):
    return tg_post(
        "sendMediaGroup",
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "media": json.dumps(media_items, ensure_ascii=False),
        },
    )


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
    """
    약후 리스트 페이지에서 wr_id 링크를 느슨하게 수집.
    board.php가 아니어도 wr_id=만 있으면 인정, bo_table 없으면 TARGET_BOARD로 강제.
    """
    r = SESSION.get(HGALL_URL, timeout=TIMEOUT)
    html = get_encoding_safe_text(r)
    soup = BeautifulSoup(html, "html.parser")

    posts = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        if "wr_id=" not in href:
            continue

        m = re.search(r"wr_id=(\d+)", href)
        if not m:
            continue
        wr_id = int(m.group(1))

        if "bo_table=" in href:
            bm = re.search(r"bo_table=([a-z0-9_]+)", href, re.I)
            bo_table = bm.group(1).lower() if bm else TARGET_BOARD
        else:
            bo_table = TARGET_BOARD

        if bo_table != TARGET_BOARD:
            continue  # yakhu 전용

        title = a.get_text(strip=True)
        if not title:
            continue

        full_url = absolutize(HGALL_URL, href)
        posts.append({"bo_table": bo_table, "wr_id": wr_id, "title": title, "url": full_url})

    # dedup by (bo, id) and sort desc
    posts = sorted({(p["bo_table"], p["wr_id"]): p for p in posts}.values(),
                   key=lambda x: x["wr_id"], reverse=True)

    print(f"[debug] 약후 리스트 수집 완료: {len(posts)}개")
    return posts


def process():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID is required")

    if ENABLE_HEARTBEAT:
        tg_send_text(HEARTBEAT_TEXT)

    posts = fetch_hgall_yakhu_list()
    posts.sort(key=lambda x: x["wr_id"])  # oldest first

    seen = load_seen()
    to_send = []
    for p in posts:
        key = f"etoland:{p['bo_table']}:{p['wr_id']}"
        if key not in seen:
            p["_seen_key"] = key
            to_send.append(p)

    # 강제 전송(테스트용)
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
        title = p["title"]
        url = p["url"]

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
                typ = "video" if any(
                    murl.lower().endswith(ext) for ext in (".mp4", ".mov", ".webm", ".mkv", ".m4v")
                ) else "photo"
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
            lines = "\n".join(iframes[:5])
            tg_send_text("\U0001F3A5 임베드 동영상 링크:\n" + lines)

        sent_keys.append(p["_seen_key"])
        time.sleep(1)

    append_seen(sent_keys)
    print(f"[info] appended {len(sent_keys)} keys")


if __name__ == "__main__":
    process()
