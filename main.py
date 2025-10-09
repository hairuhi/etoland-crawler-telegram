import os
import re
import json
import time
import pathlib
import hashlib
from urllib.parse import urljoin, urlparse, parse_qs, quote

import requests
from bs4 import BeautifulSoup

# ========= 텔레그램 환경 변수 =========
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ========= 모니터링 대상 =========
# 이토랜드
TARGET_BOARD_HUMOR = "etohumor07"  # 약후만
HUMOR_SCA_FIXED = "약후"
BASE_HUMOR_URL = f"https://www.etoland.co.kr/bbs/board.php?bo_table={TARGET_BOARD_HUMOR}"

TARGET_BOARD_STAR  = "star02"      # 전체
BASE_STAR_URL  = f"https://www.etoland.co.kr/bbs/board.php?bo_table={TARGET_BOARD_STAR}"

# AVDBS
AVDBS_BASE = "https://www.avdbs.com"
AVDBS_LISTS = [
    "/board/t50",  # 예: 공지/갤러리/인기 글 카테고리 (사이트 정책에 따라 구성 변동 가능)
    "/board/t22",
]

# ========= 상태/테스트 =========
SEEN_SET_FILE = os.getenv("SEEN_SET_FILE", "state/seen_ids.txt")  # 키 형식: etoland:bo:wr_id / avdbs:tXX:sha1(url)
ENABLE_HEARTBEAT = os.getenv("ENABLE_HEARTBEAT", "0").strip() == "1"
HEARTBEAT_TEXT = os.getenv("HEARTBEAT_TEXT", "🧪 Heartbeat: 워크플로우는 정상 동작 중입니다.")

# ========= HTTP 공통 =========
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; EtolandCrawler/3.0; +https://github.com/your/repo)",
    "Accept-Language": "ko,ko-KR;q=0.9,en;q=0.8",
    "Referer": "https://www.etoland.co.kr/",
    "Connection": "close",
})
TIMEOUT = 20

# ========= 공통 유틸 =========
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

def absolutize(base: str, url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base, url)

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def build_caption(title: str, url: str, summary: str, batch_idx: int | None, total_batches: int | None) -> str:
    # 텔레그램 캡션 제한 고려(1024), 넉넉히 900자로 컷
    prefix = f"📌 <b>{title}</b>"
    if batch_idx is not None and total_batches is not None and total_batches > 1:
        prefix += f"  ({batch_idx}/{total_batches})"
    body = f"\n{summary}" if summary else ""
    suffix = f"\n{url}"
    caption = f"{prefix}{body}{suffix}"
    if len(caption) > 900:
        caption = caption[:897] + "…"
    return caption

# ========= 이토랜드: wr_id/bo_table 파싱 =========
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

# ========= 본문 공통: 요약/미디어 추출 =========
def text_summary_from_html(soup: BeautifulSoup, max_chars: int = 280) -> str:
    candidates = [
        "#bo_v_con", ".bo_v_con", "div.view_content", ".viewContent",
        "#view_content", "article", ".xe_content", "#bd_view", ".rd_body"
    ]
    container = None
    for sel in candidates:
        found = soup.select_one(sel)
        if found:
            container = found
            break
    if container is None:
        container = soup
    for tag in container(["script", "style", "noscript"]):
        tag.extract()
    text = container.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    if len(text) > max_chars:
        return text[:max_chars - 1] + "…"
    return text

def fetch_content_media_and_summary(post_url: str) -> dict:
    r = SESSION.get(post_url, timeout=TIMEOUT)
    html = get_encoding_safe_text(r)
    soup = BeautifulSoup(html, "html.parser")

    summary = text_summary_from_html(soup, max_chars=280)

    candidates = [
        "#bo_v_con", ".bo_v_con", "div.view_content", ".viewContent",
        "#view_content", "article", ".xe_content", "#bd_view", ".rd_body"
    ]
    container = None
    for sel in candidates:
        found = soup.select_one(sel)
        if found:
            container = found
            break
    if container is None:
        container = soup

    # 이미지 (lazy 속성 포함)
    images = []
    for img in container.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original") or img.get("data-echo")
        if not src:
            continue
        images.append(absolutize(post_url, src))

    # 동영상(파일 URL만)
    video_exts = (".mp4", ".mov", ".webm", ".mkv", ".m4v")
    videos = []
    for v in container.find_all(["video", "source"]):
        src = v.get("src")
        if not src:
            continue
        src = absolutize(post_url, src)
        if any(src.lower().endswith(ext) for ext in video_exts):
            videos.append(src)

    # iframe(유튜브 등)은 텍스트 안내
    iframes = []
    for f in container.find_all("iframe"):
        src = f.get("src")
        if src:
            iframes.append(absolutize(post_url, src))

    images = list(dict.fromkeys(images))
    videos = list(dict.fromkeys(videos))
    iframes = list(dict.fromkeys(iframes))

    # 제목 보강: og:title → <title> 순
    title = None
    ogt = soup.find("meta", property="og:title")
    if ogt and ogt.get("content"):
        title = ogt.get("content").strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()

    return {"images": images, "videos": videos, "iframes": iframes, "summary": summary, "title_override": title}

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

# ========= 이토랜드 목록 =========
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
            posts[key] = {"source": "etoland", "bo_table": bo, "wr_id": wr, "title": title, "url": link}
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
            posts[key] = {"source": "etoland", "bo_table": bo, "wr_id": wr, "title": title, "url": link}
    res = sorted(posts.values(), key=lambda x: x["wr_id"], reverse=True)
    print(f"[debug] star list fetched: {len(res)} items")
    return res

# ========= AVDBS 목록 =========
def fetch_avdbs_list(list_path: str) -> list[dict]:
    """리스트 페이지에서 같은 도메인의 게시물 링크 수집 (일반화)"""
    base_url = urljoin(AVDBS_BASE, list_path)
    r = SESSION.get(base_url, timeout=TIMEOUT)
    html = get_encoding_safe_text(r)
    soup = BeautifulSoup(html, "html.parser")

    posts = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        absu = absolutize(base_url, href)
        u = urlparse(absu)
        if u.netloc != urlparse(AVDBS_BASE).netloc:
            continue
        # 리스트 자기 자신/페이지네이션/앵커 제외
        if u.path.rstrip("/") == urlparse(base_url).path.rstrip("/"):
            continue
        if "page=" in u.query.lower():
            continue
        # 게시물 같은 링크만 추정: 경로에 /board/ 포함
        if "/board/" not in u.path:
            continue

        # 제목 후보
        title = a.get_text(strip=True)
        if not title:
            title = (u.path.rstrip("/").split("/")[-1] or absu)
        key = sha1(absu)
        # 리스트 식별자(t코드)
        tcode = (urlparse(list_path).path.rstrip("/").split("/")[-1] or "tX").lower()

        if key not in posts or (title and len(title) > len(posts[key]["title"])):
            posts[key] = {
                "source": "avdbs",
                "tcode": tcode,
                "url": absu,
                "title": title
            }
    res = list(posts.values())
    print(f"[debug] avdbs {list_path} fetched: {len(res)} items")
    return res

# ========= 메인 =========
def process():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 환경변수가 필요합니다.")

    if ENABLE_HEARTBEAT:
        tg_send_text(HEARTBEAT_TEXT)

    # 1) 이토랜드 수집
    posts_humor = fetch_humor_약후_list()
    posts_star  = fetch_star_list()

    # 2) AVDBS 수집
    posts_avdbs = []
    for lp in AVDBS_LISTS:
        try:
            posts_avdbs.extend(fetch_avdbs_list(lp))
        except Exception as e:
            print(f"[warn] avdbs list fetch failed: {lp} err={e}")

    # 병합
    merged = {}
    # 이토랜드 키: ("etoland", bo, wr)
    for p in posts_humor + posts_star:
        key = ("etoland", p["bo_table"], p["wr_id"])
        if key not in merged:
            merged[key] = p
    # AVDBS 키: ("avdbs", tcode, sha1(url))
    for p in posts_avdbs:
        key = ("avdbs", p["tcode"], sha1(p["url"]))
        if key not in merged:
            merged[key] = p

    posts = list(merged.values())

    # 정렬: 소스별 보조키로 오래된 것부터 추정 정렬
    def sort_key(p):
        if p.get("source") == "etoland":
            return (0, p["bo_table"], p["wr_id"])
        # avdbs는 wr_id가 없으므로 tcode와 URL 해시 일부 기준(안정성용)
        return (1, p["tcode"], p["url"])
    posts.sort(key=sort_key)

    print(f"[debug] merged items: {len(posts)}")

    # 이미 본 항목 제외
    seen = load_seen()
    to_send = []
    for p in posts:
        if p.get("source") == "etoland":
            key = f"etoland:{p['bo_table']}:{p['wr_id']}"
        else:
            key = f"avdbs:{p['tcode']}:{sha1(p['url'])}"
        if key not in seen:
            p["_seen_key"] = key
            to_send.append(p)

    if not to_send:
        print("[info] 새 글 없음.")
        return

    # 전송
    sent_keys = []
    for p in to_send:
        source = p.get("source")
        title = p["title"]
        url   = p["url"]

        media = fetch_content_media_and_summary(url)
        # 제목 보강(본문 og:title이 더 정제된 경우)
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
            tg_send_text("🎬 임베드 동영상 링크:\n" + "\n".join(iframes[:5]))

        sent_keys.append(p["_seen_key"])
        time.sleep(1)

    append_seen(sent_keys)
    print(f"[info] appended {len(sent_keys)} new keys to seen set")

if __name__ == "__main__":
    process()
