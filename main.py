import os
import re
import time
import pathlib
from urllib.parse import urljoin, urlparse, parse_qs, quote

import requests
from bs4 import BeautifulSoup

# ========= 텔레그램 환경 변수 =========
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ========= 모니터링 대상 (요청사항 반영) =========
# 1) 유머게시판: '약후' 카테고리만 전송 (코드에 고정)
TARGET_BOARD_HUMOR = "etohumor07"
HUMOR_SCA_FIXED = "약후"
BASE_HUMOR_URL = f"https://www.etoland.co.kr/bbs/board.php?bo_table={TARGET_BOARD_HUMOR}"

# 2) 연예인 게시판: 카테고리 없이 전체 전송
TARGET_BOARD_STAR = "star02"
BASE_STAR_URL = f"https://www.etoland.co.kr/bbs/board.php?bo_table={TARGET_BOARD_STAR}"

# ========= 상태/테스트 설정 =========
SEEN_SET_FILE = os.getenv("SEEN_SET_FILE", "state/seen_ids.txt")  # bo_table:wr_id 형식으로 기록
ENABLE_HEARTBEAT = os.getenv("ENABLE_HEARTBEAT", "0").strip() == "1"
HEARTBEAT_TEXT = os.getenv("HEARTBEAT_TEXT", "🧪 Heartbeat: 워크플로우는 정상 동작 중입니다.")

# ========= HTTP 세션 공통 =========
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; EtolandCrawler/2.0; +https://github.com/your/repo)",
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

# wr_id/bo_table 파싱(PC/모바일 URL 모두 허용)
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
    # fallback: 쿼리 파싱
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

# ========= 목록/본문 파싱 =========
def fetch_humor_약후_list() -> list[dict]:
    """유머게시판 '약후' 카테고리 전용 목록"""
    url = f"{BASE_HUMOR_URL}&sca={euckr_quote(HUMOR_SCA_FIXED)}"
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
    """연예인 게시판 전체 목록"""
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

def fetch_content_media(post_url: str) -> dict:
    r = SESSION.get(post_url, timeout=TIMEOUT)
    html = get_encoding_safe_text(r)
    soup = BeautifulSoup(html, "html.parser")

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
        src = absolutize(post_url, src)
        if src.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            images.append(src)

    videos = []
    for v in container.find_all(["video", "source", "iframe"]):
        src = v.get("src")
        if not src:
            continue
        src = absolutize(post_url, src)
        videos.append(src)

    return {"images": images[:5], "videos": videos[:3]}

# ========= 텔레그램 전송(응답 로그 포함) =========
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

def tg_send_photo(photo_url: str, caption: str | None = None):
    return tg_post("sendPhoto", {
        "chat_id": TELEGRAM_CHAT_ID,
        "caption": caption or "",
        "photo": photo_url
    })

def tg_send_video(video_url: str, caption: str | None = None):
    return tg_post("sendVideo", {
        "chat_id": TELEGRAM_CHAT_ID,
        "caption": caption or "",
        "video": video_url
    })

# ========= 메인 로직 =========
def process():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 환경변수가 필요합니다.")

    if ENABLE_HEARTBEAT:
        tg_send_text(HEARTBEAT_TEXT)

    # 1) 유머(약후) + 2) 연예인 목록 가져오기
    posts_humor = fetch_humor_약후_list()
    posts_star  = fetch_star_list()

    # 병합 & dedup
    merged = {}
    for p in posts_humor + posts_star:
        key = (p["bo_table"], p["wr_id"])
        if key not in merged:
            merged[key] = p

    posts = list(merged.values())
    posts.sort(key=lambda x: (x["bo_table"], x["wr_id"]))  # 오래된 것부터 전송

    print(f"[debug] merged items: {len(posts)}")

    # 이미 본 항목 제외
    seen = load_seen()
    to_send = []
    for p in posts:
        key = f"{p['bo_table']}:{p['wr_id']}"
        if key not in seen:
            to_send.append(p)

    if not to_send:
        print("[info] 새 글 없음.")
        return

    # 전송
    sent_keys = []
    for p in to_send:
        bo = p["bo_table"]
        wr = p["wr_id"]
        title = p["title"]
        url = p["url"]
        header = f"📌 <b>[{bo}] {title}</b>\n{url}"

        media = fetch_content_media(url)
        sent_any = False

        if media["images"]:
            tg_send_photo(media["images"][0], caption=header)
            sent_any = True
            extra = len(media["images"]) - 1
            if extra > 0:
                tg_send_text(f"🖼 추가 이미지 {extra}장 더 있음 → 원문 링크 확인")

        if media["videos"]:
            r, j = tg_send_video(media["videos"][0], caption=f"🎬 동영상(1/?)\n{url}")
            if not j.get("ok"):
                tg_send_text(f"🎬 동영상 링크: {media['videos'][0]}")
            sent_any = True

        if not sent_any:
            tg_send_text(header)

        sent_keys.append(f"{bo}:{wr}")
        time.sleep(1)  # 예절상 대기

    append_seen(sent_keys)
    print(f"[info] appended {len(sent_keys)} new keys to seen set")

if __name__ == "__main__":
    process()
