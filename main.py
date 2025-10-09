import os
import re
import time
import pathlib
from urllib.parse import urljoin, urlparse, parse_qs, quote

import requests
from bs4 import BeautifulSoup

# === 환경변수 (GitHub Secrets/Variables 로 주입) ===
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 모니터링 대상 (기본: 유머게시판 전체)
# 특정 카테고리(예: '약후')만 보고 싶다면 ETO_SCA_KO="약후" 로 설정
BASE_LIST_URL = "https://www.etoland.co.kr/bbs/board.php?bo_table=etohumor07"
ETO_SCA_KO = os.getenv("ETO_SCA_KO", "약후").strip()

# 상태 파일 (가장 최근 전송한 wr_id 저장 → 재전송 방지)
STATE_FILE = os.getenv("STATE_FILE", "state/last_id.txt")

# 요청 공통 옵션
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; EtolandCrawler/1.0; +https://github.com/your/repo)",
    "Accept-Language": "ko,ko-KR;q=0.9,en;q=0.8",
    "Referer": "https://www.etoland.co.kr/",
    "Connection": "close",
})
TIMEOUT = 15

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
    # sca 파라미터가 EUC-KR 인코딩을 기대할 수 있어 대비
    try:
        return quote(s.encode("euc-kr"))
    except Exception:
        return quote(s)

def build_list_url() -> str:
    if ETO_SCA_KO:
        return f"{BASE_LIST_URL}&sca={euckr_quote(ETO_SCA_KO)}"
    return BASE_LIST_URL

def get_encoding_safe_text(resp: requests.Response) -> str:
    # 일부 페이지가 EUC-KR일 수 있어, 서버 힌트/추정 인코딩 반영
    if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ansi_x3.4-1968"):
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text

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
    # fallback: 쿼리 파싱
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

def fetch_list() -> list[dict]:
    """목록 페이지에서 wr_id/제목/URL 추출 (DOM 의존 최소화, 정규식 기반)"""
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
        # 가장 보기 좋은 제목/링크만 보존
        if wr_id not in posts or (title and len(title) > len(posts[wr_id]["title"])):
            posts[wr_id] = {"wr_id": wr_id, "title": title, "url": link}

    # wr_id 내림차순(최신 먼저)
    return sorted(posts.values(), key=lambda x: x["wr_id"], reverse=True)

def fetch_content_media(post_url: str) -> dict:
    """본문에서 대표 이미지/영상 링크 일부를 수집"""
    r = SESSION.get(post_url, timeout=TIMEOUT)
    html = get_encoding_safe_text(r)
    soup = BeautifulSoup(html, "html.parser")

    # 일반적으로 그누보드 본문 컨테이너들
    candidates = [
        "#bo_v_con",              # 기본 그누보드
        ".bo_v_con",
        "div.view_content",       # 커스텀 테마
        ".viewContent",
        "#view_content",
        "article",                # 광범위 대응
    ]
    container = None
    for sel in candidates:
        found = soup.select_one(sel)
        if found:
            container = found
            break
    if container is None:
        container = soup  # 최후 수단

    images = []
    for img in container.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        src = absolutize(post_url, src)
        if src.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            images.append(src)

    videos = []
    # <video src>, <source src>, <iframe src>(유튜브 등)
    for v in container.find_all(["video", "source", "iframe"]):
        src = v.get("src")
        if not src:
            continue
        src = absolutize(post_url, src)
        videos.append(src)

    return {"images": images[:5], "videos": videos[:3]}

def tg_send_text(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
    SESSION.post(url, data=data, timeout=TIMEOUT)

def tg_send_photo(photo_url: str, caption: str | None = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption or ""}
    # Telegram은 URL 전송 지원 (파일 업로드 불필요)
    data["photo"] = photo_url
    SESSION.post(url, data=data, timeout=TIMEOUT)

def tg_send_video(video_url: str, caption: str | None = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendVideo"
    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption or "", "video": video_url}
    SESSION.post(url, data=data, timeout=TIMEOUT)

def process():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 환경변수가 필요합니다.")

    last_id = read_last_id()
    print(f"[info] last_id(state)={last_id}")

    posts = fetch_list()
    if not posts:
        print("[warn] 목록을 찾지 못했습니다.")
        return

    # 최신글부터, last_id 보다 큰 것만 전송
    new_posts = [p for p in posts if p["wr_id"] > last_id]
    if not new_posts:
        print("[info] 새 글 없음.")
        return

    # 오래된 글부터 순서대로 보내기(타임라인 보존)
    new_posts_sorted = sorted(new_posts, key=lambda x: x["wr_id"])

    sent_max_id = last_id
    for p in new_posts_sorted:
        title = p["title"]
        url = p["url"]
        msg_header = f"📌 <b>{title}</b>\n{url}"

        media = fetch_content_media(url)
        sent_any = False

        # 1) 대표 이미지(최대 1장) 우선
        if media["images"]:
            tg_send_photo(media["images"][0], caption=msg_header)
            sent_any = True
            # 추가 이미지가 있으면 링크로만 안내
            extra = len(media["images"]) - 1
            if extra > 0:
                tg_send_text(f"🖼 추가 이미지 {extra}장 더 있음 → 원문 링크 확인")

        # 2) 동영상 링크가 있으면 첫 개 전송 시도(텔레그램이 직접 재생 가능한 URL만 미리보기)
        if media["videos"]:
            try:
                tg_send_video(media["videos"][0], caption=f"🎬 동영상(1/?)\n{url}")
                sent_any = True
            except Exception:
                # 일부 iframe(유튜브)은 sendVideo 불가 → 텍스트 링크로 대체
                tg_send_text(f"🎬 동영상 링크: {media['videos'][0]}")

        # 3) 아무 미디어도 없으면 텍스트만
        if not sent_any:
            tg_send_text(msg_header)

        sent_max_id = max(sent_max_id, p["wr_id"])
        time.sleep(1)  # 예의상 rate-limit

    if sent_max_id > last_id:
        write_last_id(sent_max_id)
        print(f"[info] state updated: last_id={sent_max_id}")

if __name__ == "__main__":
    process()
