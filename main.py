import os
import re
import time
import pathlib
from urllib.parse import urljoin, urlparse, parse_qs, quote

import requests
from bs4 import BeautifulSoup

# ========= 환경 변수 =========
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 기본: 유머게시판 전체. 특정 카테고리만(예: "약후") 보고 싶으면 저장소 Variables에 ETO_SCA_KO=약후 등록
BASE_LIST_URL = "https://www.etoland.co.kr/bbs/board.php?bo_table=etohumor07"
ETO_SCA_KO = os.getenv("ETO_SCA_KO", "").strip()

# 재전송 방지용 상태 파일 (저장소에 커밋됨)
STATE_FILE = os.getenv("STATE_FILE", "state/last_id.txt")

# 하트비트(테스트용) — 1로 설정하면 실행할 때마다 “동작 확인” 메시지를 보냄
ENABLE_HEARTBEAT = os.getenv("ENABLE_HEARTBEAT", "0").strip() == "1"
HEARTBEAT_TEXT = os.getenv("HEARTBEAT_TEXT", "🧪 Heartbeat: 워크플로우는 정상 동작 중입니다.")

# ========= HTTP 세션 공통 =========
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; EtolandCrawler/1.0; +https://github.com/your/repo)",
    "Accept-Language": "ko,ko-KR;q=0.9,en;q=0.8",
    "Referer": "https://www.etoland.co.kr/",
    "Connection": "close",
})
TIMEOUT = 15

# ========= 유틸 =========
def ensure_state_dir():
    pathlib.Path(os.path.dirname(STATE_FILE) or ".").mkdir(parents=True, exist_ok=True)

def read_last_id() -> int:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return 0

def write_last_id(wr_id: int):
    ensure_state_dir()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(str(wr_id))

def euckr_quote(s: str) -> str:
    try:
        return quote(s.encode("euc-kr"))
    except Exception:
        return quote(s)

def build_list_url() -> str:
    if ETO_SCA_KO:
        return f"{BASE_LIST_URL}&sca={euckr_quote(ETO_SCA_KO)}"
    return BASE_LIST_URL

def get_encoding_safe_text(resp: requests.Response) -> str:
    if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ansi_x3.4-1968"):
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text

# wr_id 파싱(PC/모바일 URL 모두 허용)
WR_ID_RE = re.compile(r"(?:board\.php|plugin/mobile/board\.php)\?[^\"'>]*\bbo_table=etohumor07\b[^\"'>]*\bwr_id=(\d+)", re.I)

def extract_wr_id_from_href(href: str) -> int | None:
    if not href:
        return None
    m = WR_ID_RE.search(href)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    # 쿼리 파싱 보강
    try:
        q = parse_qs(urlparse(href).query)
        if "wr_id" in q:
            return int(q["wr_id"][0])
    except Exception:
        pass
    return None

def absolutize(base: str, url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base, url)

# ========= 목록/본문 파싱 =========
def fetch_list() -> list[dict]:
    url = build_list_url()
    r = SESSION.get(url, timeout=TIMEOUT)
    html = get_encoding_safe_text(r)
    soup = BeautifulSoup(html, "html.parser")

    posts = {}
    for a in soup.find_all("a", href=True):
        wr_id = extract_wr_id_from_href(a["href"])
        if not wr_id:
            continue
        title = a.get_text(strip=True) or f"글번호 {wr_id}"
        link = absolutize(url, a["href"])
        if wr_id not in posts or (title and len(title) > len(posts[wr_id]["title"])):
            posts[wr_id] = {"wr_id": wr_id, "title": title, "url": link}

    return sorted(posts.values(), key=lambda x: x["wr_id"], reverse=True)

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

    last_id = read_last_id()
    print(f"[info] last_id(state)={last_id}")

    posts = fetch_list()
    print(f"[debug] fetched posts count={len(posts)}")
    if posts[:3]:
        print("[debug] sample posts:", posts[:3])

    if not posts:
        print("[warn] 목록 파싱 실패 또는 게시물 없음")
        return

    new_posts = [p for p in posts if p["wr_id"] > last_id]
    if not new_posts:
        print("[info] 새 글 없음.")
        return

    new_posts_sorted = sorted(new_posts, key=lambda x: x["wr_id"])

    sent_max_id = last_id
    for p in new_posts_sorted:
        title = p["title"]
        url = p["url"]
        header = f"📌 <b>{title}</b>\n{url}"

        media = fetch_content_media(url)
        sent_any = False

        if media["images"]:
            tg_send_photo(media["images"][0], caption=header)
            sent_any = True
            extra = len(media["images"]) - 1
            if extra > 0:
                tg_send_text(f"🖼 추가 이미지 {extra}장 더 있음 → 원문 링크 확인")

        if media["videos"]:
            # 일부는 직접 재생 불가일 수 있음(특히 iframe). 실패 시 링크로 대체.
            r, j = tg_send_video(media["videos"][0], caption=f"🎬 동영상(1/?)\n{url}")
            if not j.get("ok"):
                tg_send_text(f"🎬 동영상 링크: {media['videos'][0]}")
            sent_any = True

        if not sent_any:
            tg_send_text(header)

        sent_max_id = max(sent_max_id, p["wr_id"])
        time.sleep(1)  # 예절상 대기

    if sent_max_id > last_id:
        write_last_id(sent_max_id)
        print(f"[info] state updated: last_id={sent_max_id}")

if __name__ == "__main__":
    process()
