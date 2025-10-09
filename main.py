import os
import re
import json
import time
import pathlib
from urllib.parse import urljoin, urlparse, parse_qs, quote

import requests
from bs4 import BeautifulSoup

# ========= 텔레그램 환경 변수 =========
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ========= 모니터링 대상 =========
# 1) 유머게시판: '약후' 카테고리만 (코드 고정)
TARGET_BOARD_HUMOR = "etohumor07"
HUMOR_SCA_FIXED = "약후"
BASE_HUMOR_URL = f"https://www.etoland.co.kr/bbs/board.php?bo_table={TARGET_BOARD_HUMOR}"

# 2) 연예인 게시판: 전체
TARGET_BOARD_STAR = "star02"
BASE_STAR_URL = f"https://www.etoland.co.kr/bbs/board.php?bo_table={TARGET_BOARD_STAR}"

# ========= 상태/테스트 설정 =========
SEEN_SET_FILE = os.getenv("SEEN_SET_FILE", "state/seen_ids.txt")  # bo_table:wr_id 기록
ENABLE_HEARTBEAT = os.getenv("ENABLE_HEARTBEAT", "0").strip() == "1"
HEARTBEAT_TEXT = os.getenv("HEARTBEAT_TEXT", "🧪 Heartbeat: 워크플로우는 정상 동작 중입니다.")

# ========= HTTP 공통 =========
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; EtolandCrawler/2.2; +https://github.com/your/repo)",
    "Accept-Language": "ko,ko-KR;q=0.9,en;q=0.8",
    "Referer": "https://www.etoland.co.kr/",
    "Connection": "close",
})
TIMEOUT = 15

# ========= 유틸 =========
def ensure_state_dir():
    pathlib.Path("state").mkdir(parents=True, exist_ok=True)

def load_seen() -> set:
    ensure_state_dir()
    s = set()
    p = pathlib.Path(SEEN_SET_FILE)
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
    with open(SEEN_SET_FILE, "a", encoding="utf-8") as f:
        for k in keys:
            f.write(k + "\n")

def euckr_quote(s: str) -> str:
    try:
        return quote(s.encode("euc-kr"))
    except Exception:
        return quote(s)

def get_encoding_safe_text(resp: requests.Response) -> str:
    if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ansi_x3.4-1968"):
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text

def text_summary_from_html(soup: BeautifulSoup, max_chars: int = 280) -> str:
    # 본문 컨테이너 추려서 텍스트 요약
    candidates = [
        "#bo_v_con", ".bo_v_con", "div.view_content", ".viewContent",
        "#view_content", "article"
    ]
    container = None
    for sel in candidates:
        found = soup.select_one(sel)
        if found:
            container = found
            break
    if container is None:
        container = soup

    # 이미지/스크립트/스타일 제거된 텍스트
    for tag in container(["script", "style", "noscript"]):
        tag.extract()
    text = container.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    if len(text) > max_chars:
        return text[:max_chars - 1] + "…"
    return text

# wr_id/bo_table 파싱
LINK_RE = re.compile(
    r"(?:board\.php|plugin/mobile/board\.php)\?[^\"'>]*\bbo_table=([a-z0-9_]+)\b[^\"'>]*\bwr_id=(\d+)",
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
        q = parse_qs(urlparse(href).query)
        bo = (q.get("bo_table", [""])[0] or "").lower()
        wr = int(q.get("wr_id", ["0"])[0])
        return bo or None, wr or None
    except Exception:
        return None, None

def absolutize(base: str, url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base, url)

# ========= 목록 수집 =========
def fetch_humor_약후_list() -> list[dict]:
    url = f"{BASE_HUMOR_URL}&sca={euckr_quote('약후')}"
    r = SESSION.get(url, timeout=TIMEOUT)
    html = get_encoding_safe_text(r)
    soup = BeautifulSoup(html, "html.parser")

    posts = {}
    for a in soup.find_all("a", href=True):
        bo, wr = extract_bo_and_id(a["href"])
        if bo != TARGET_BOARD_HUMOR or not wr:
            continue
        title = a.get_text(strip=True) or f"[{bo}] 글번호 {wr}"
        link = absolutize(url, a["href"])
        key = (bo, wr)
        if key not in posts or (title and len(title) > len(posts[key]["title"])):
            posts[key] = {"bo_table": bo, "wr_id": wr, "title": title, "url": link}

    res = sorted(posts.values(), key=lambda x: x["wr_id"], reverse=True)
    print(f"[debug] humor(약후) list fetched: {len(res)} items")
    return res

def fetch_star_list() -> list[dict]:
    url = BASE_STAR_URL
    r = SESSION.get(url, timeout=TIMEOUT)
    html = get_encoding_safe_text(r)
    soup = BeautifulSoup(html, "html.parser")

    posts = {}
    for a in soup.find_all("a", href=True):
        bo, wr = extract_bo_and_id(a["href"])
        if bo != TARGET_BOARD_STAR or not wr:
            continue
        title = a.get_text(strip=True) or f"[{bo}] 글번호 {wr}"
        link = absolutize(url, a["href"])
        key = (bo, wr)
        if key not in posts or (title and len(title) > len(posts[key]["title"])):
            posts[key] = {"bo_table": bo, "wr_id": wr, "title": title, "url": link}

    res = sorted(posts.values(), key=lambda x: x["wr_id"], reverse=True)
    print(f"[debug] star list fetched: {len(res)} items")
    return res

# ========= 본문 미디어/요약 =========
def fetch_content_media_and_summary(post_url: str) -> dict:
    r = SESSION.get(post_url, timeout=TIMEOUT)
    html = get_encoding_safe_text(r)
    soup = BeautifulSoup(html, "html.parser")

    # 요약
    summary = text_summary_from_html(soup, max_chars=280)

    # 컨테이너 재활용
    candidates = [
        "#bo_v_con", ".bo_v_con", "div.view_content", ".viewContent",
        "#view_content", "article"
    ]
    container = None
    for sel in candidates:
        found = soup.select_one(sel)
        if found:
            container = found
            break
    if container is None:
        container = soup

    images = []
    for img in container.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        images.append(absolutize(post_url, src))

    # 직접 파일 동영상만(그룹 전송 가능)
    video_exts = (".mp4", ".mov", ".webm", ".mkv", ".m4v")
    videos = []
    for v in container.find_all(["video", "source"]):
        src = v.get("src")
        if not src:
            continue
        src = absolutize(post_url, src)
        if any(src.lower().endswith(ext) for ext in video_exts):
            videos.append(src)

    # iframe(유튜브 등) 링크는 텍스트로 안내
    iframes = []
    for f in container.find_all("iframe"):
        src = f.get("src")
        if src:
            iframes.append(absolutize(post_url, src))

    # 중복 제거
    images = list(dict.fromkeys(images))
    videos = list(dict.fromkeys(videos))
    iframes = list(dict.fromkeys(iframes))

    return {"images": images, "videos": videos, "iframes": iframes, "summary": summary}

# ========= 텔레그램 전송 =========
def tg_post(method: str, data: dict):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    r = SESSION.post(url, data=data, timeout=TIMEOUT)
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

# ========= 메인 =========
def build_caption(title: str, url: str, summary: str, batch_idx: int | None, total_batches: int | None) -> str:
    # 텔레그램 캡션 최대 1024자. 넉넉히 900자로 제한.
    prefix = f"📌 <b>{title}</b>"
    if batch_idx is not None and total_batches is not None and total_batches > 1:
        prefix += f"  ({batch_idx}/{total_batches})"
    body = f"\n{summary}" if summary else ""
    suffix = f"\n{url}"
    caption = f"{prefix}{body}{suffix}"
    if len(caption) > 900:
        caption = caption[:897] + "…"
    return caption

def process():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 환경변수가 필요합니다.")

    if ENABLE_HEARTBEAT:
        tg_send_text(HEARTBEAT_TEXT)

    posts_humor = fetch_humor_약후_list()
    posts_star  = fetch_star_list()

    merged = {}
    for p in posts_humor + posts_star:
        key = (p["bo_table"], p["wr_id"])
        if key not in merged:
            merged[key] = p

    posts = list(merged.values())
    posts.sort(key=lambda x: (x["bo_table"], x["wr_id"]))  # 오래된 것부터

    print(f"[debug] merged items: {len(posts)}")

    seen = load_seen()
    to_send = []
    for p in posts:
        key = f"{p['bo_table']}:{p['wr_id']}"
        if key not in seen:
            to_send.append(p)

    if not to_send:
        print("[info] 새 글 없음.")
        return

    sent_keys = []
    for p in to_send:
        bo = p["bo_table"]
        wr = p["wr_id"]
        title = p["title"]
        url = p["url"]

        media = fetch_content_media_and_summary(url)
        images = media["images"]
        videos = media["videos"]
        iframes = media["iframes"]
        summary = media["summary"]

        # 미디어 합치고 10개씩 배치
        media_urls = images + videos  # 사진 우선, 뒤에 동영상
        if not media_urls:
            # 미디어가 없으면 텍스트만 (제목+요약+링크)
            caption = build_caption(title, url, summary, None, None)
            tg_send_text(caption)
            sent_keys.append(f"{bo}:{wr}")
            time.sleep(1)
            continue

        MAX_ITEMS = 10
        total = len(media_urls)
        total_batches = (total + MAX_ITEMS - 1) // MAX_ITEMS

        for batch_idx in range(total_batches):
            start = batch_idx * MAX_ITEMS
            end = min(start + MAX_ITEMS, total)
            chunk = media_urls[start:end]

            media_items = []
            for i, murl in enumerate(chunk):
                # 동영상 확장자면 video, 아니면 photo로 보냄
                typ = "video" if any(murl.lower().endswith(ext) for ext in (".mp4", ".mov", ".webm", ".mkv", ".m4v")) else "photo"
                item = {"type": typ, "media": murl}
                # 첫 배치의 첫 항목에만 캡션 달기
                if batch_idx == 0 and i == 0:
                    item["caption"] = build_caption(title, url, summary, batch_idx + 1, total_batches)
                    item["parse_mode"] = "HTML"
                # 두 번째 이후 배치의 첫 항목에는 간단 캡션
                elif i == 0 and total_batches > 1:
                    item["caption"] = f"({batch_idx + 1}/{total_batches}) 계속"
                media_items.append(item)

            r, j = tg_send_media_group(media_items)
            if not j.get("ok"):
                # 실패 시 텍스트로 폴백
                tg_send_text(build_caption(title, url, summary, batch_idx + 1, total_batches))
            time.sleep(1)

        # iframe 안내 (유튜브 등)
        if iframes:
            tg_send_text("🎬 임베드 동영상 링크:\n" + "\n".join(iframes[:5]))

        # 남는 미디어(배치 외)는 없음 — 이미 배치로 모두 전송
        sent_keys.append(f"{bo}:{wr}")
        time.sleep(1)

    append_seen(sent_keys)
    print(f"[info] appended {len(sent_keys)} new keys to seen set")

if __name__ == "__main__":
    process()
