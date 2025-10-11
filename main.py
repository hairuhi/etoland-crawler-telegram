import os
import re
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
    "/img/icon_link.gif",
    "icon_link.gif",
    "/img/loading_img.jpg",   # 🔥 로딩 플레이스홀더 차단
    "loading_img.jpg"
]
_extra = os.getenv("EXCLUDE_IMAGE_SUBSTRINGS", "").strip()
if _extra:
    EXCLUDE_IMAGE_SUBSTRINGS += [s.strip() for s in _extra.split(",") if s.strip()]

# ========= HTTP =========
SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (compatible; EtolandYakhuOnly/1.7)",
        "Accept-Language": "ko,ko-KR;q=0.9,en;q=0.8",
        "Referer": "https://www.etoland.co.kr/",
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
        with open(p, "r", encoding="utf-8") as f:
            s = {line.strip() for line in f if line.strip()}
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

# --- Placeholder Filtering ---
PLACEHOLDER_ICON_NAMES = {"icon_link.gif", "loading_img.jpg"}

def is_placeholder_image(url: str) -> bool:
    """에토랜드 기본 아이콘/로딩 이미지 차단"""
    try:
        path = urlparse(url).path.lower()
        filename = path.rsplit("/", 1)[-1]
        return (
            filename in PLACEHOLDER_ICON_NAMES
            or "/img/icon_link.gif" in path
            or "/img/loading_img.jpg" in path
        )
    except Exception:
        return False

def is_excluded_image(url: str) -> bool:
    low = url.lower()
    return any(hint.lower() in low for hint in EXCLUDE_IMAGE_SUBSTRINGS)

# --- Telegram ---
def tg_post(method: str, data: dict, files=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    r = requests.post(url, data=data, files=files, timeout=60)
    try:
        j = r.json()
    except Exception:
        j = {"non_json_body": r.text[:300]}
    print(f"[tg] {method} {r.status_code} ok={j.get('ok')} desc={j.get('description')}")
    return r, j

def tg_send_text(text: str):
    return tg_post("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"})

def build_caption(title: str, url: str, summary: str) -> str:
    cap = f"📌 <b>{title}</b>"
    if summary:
        cap += f"\n{summary}"
    cap += f"\n{url}"
    return cap[:900]

# --- Parser ---
def text_summary_from_html(soup: BeautifulSoup, max_chars=280) -> str:
    cands = ["#bo_v_con", ".bo_v_con", "div.view_content", ".viewContent", "#view_content", "article"]
    cont = next((soup.select_one(s) for s in cands if soup.select_one(s)), soup)
    for t in cont(["script", "style", "noscript"]): t.extract()
    text = re.sub(r"\s+", " ", cont.get_text(" ", strip=True)).strip()
    return text[:max_chars-1]+"…" if len(text)>max_chars else text

def fetch_hgall_yakhu_list() -> list[dict]:
    r = SESSION.get(HGALL_URL, timeout=TIMEOUT)
    soup = BeautifulSoup(get_encoding_safe_text(r), "html.parser")
    posts=[]
    for a in soup.find_all("a", href=True):
        href=a["href"].strip()
        if "wr_id=" not in href: continue
        m=re.search(r"wr_id=(\d+)", href)
        if not m: continue
        wr_id=int(m.group(1))
        bo=TARGET_BOARD
        b=re.search(r"bo_table=([a-z0-9_]+)", href,re.I)
        if b: bo=b.group(1).lower()
        if bo!=TARGET_BOARD: continue
        t=a.get_text(strip=True)
        if not t: continue
        posts.append({"bo_table":bo,"wr_id":wr_id,"title":t,"url":absolutize(HGALL_URL,href)})
    posts=sorted({(p["bo_table"],p["wr_id"]):p for p in posts}.values(),key=lambda x:x["wr_id"],reverse=True)
    print(f"[debug] 약후 리스트 수집 완료: {len(posts)}개")
    return posts

def fetch_content_media_and_summary(post_url:str)->dict:
    r=SESSION.get(post_url,timeout=TIMEOUT)
    soup=BeautifulSoup(get_encoding_safe_text(r),"html.parser")
    summary=text_summary_from_html(soup)
    cont=next((soup.select_one(s) for s in ["#bo_v_con",".bo_v_con","div.view_content",".viewContent","#view_content","article"] if soup.select_one(s)),soup)
    all_imgs=[absolutize(post_url,img.get("src") or "") for img in cont.find_all("img") if img.get("src")]
    if TRACE_IMAGE_DEBUG: print("[trace] before-filter:",all_imgs[:10])
    images=[]
    for u in all_imgs:
        if is_placeholder_image(u): continue
        if not is_excluded_image(u): images.append(u)
    if not images:
        for a in cont.find_all("a",href=True):
            href=a["href"].strip()
            if re.search(r"\.(jpg|jpeg|png|gif|webp)(?:\?|$)",href,re.I):
                full=absolutize(post_url,href)
                if is_placeholder_image(full): continue
                if not is_excluded_image(full): images.append(full)
    if TRACE_IMAGE_DEBUG:
        print("[trace] after-filter :",images[:10])
        try: tg_send_text("🔍 image candidates:\n"+"\n".join(images[:10] or ["(no images)"]))
        except: pass
    videos=[absolutize(post_url,v.get("src")) for v in cont.find_all(["video","source"]) if v.get("src")]
    iframes=[absolutize(post_url,f.get("src")) for f in cont.find_all("iframe") if f.get("src")]
    title=(soup.find("meta",property="og:title") or {}).get("content") or (soup.title.string.strip() if soup.title else "")
    return {"images":images,"videos":videos,"iframes":iframes,"summary":summary,"title_override":title}

# --- Sending ---
def download_bytes(url:str,ref:str)->bytes|None:
    try:
        r=SESSION.get(url,headers={"Referer":ref},timeout=TIMEOUT)
        if r.status_code==200 and r.content: return r.content
    except Exception as e: print(f"[warn] download failed {url} {e}")
    return None

def send_photo_url_or_file(url:str,caption:str|None,ref:str):
    if DOWNLOAD_AND_UPLOAD:
        data=download_bytes(url,ref)
        if data:
            return tg_post("sendPhoto",{"chat_id":TELEGRAM_CHAT_ID,"caption":caption or "","parse_mode":"HTML"},
                           files={"photo":("image.jpg",BytesIO(data))})
    return tg_post("sendPhoto",{"chat_id":TELEGRAM_CHAT_ID,"photo":url,"caption":caption or "","parse_mode":"HTML"})

def send_video_url_or_file(url:str,caption:str|None,ref:str):
    if DOWNLOAD_AND_UPLOAD:
        data=download_bytes(url,ref)
        if data:
            return tg_post("sendVideo",{"chat_id":TELEGRAM_CHAT_ID,"caption":caption or "","parse_mode":"HTML"},
                           files={"video":("video.mp4",BytesIO(data))})
    return tg_post("sendVideo",{"chat_id":TELEGRAM_CHAT_ID,"video":url,"caption":caption or "","parse_mode":"HTML"})

# --- Main ---
def process():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID required")
    if ENABLE_HEARTBEAT: tg_send_text(HEARTBEAT_TEXT)
    posts=fetch_hgall_yakhu_list(); posts.sort(key=lambda x:x["wr_id"])
    seen=load_seen(); to_send=[]
    for p in posts:
        k=f"etoland:{p['bo_table']}:{p['wr_id']}"
        if k not in seen: p["_seen_key"]=k; to_send.append(p)
    if FORCE_SEND_LATEST and not to_send and posts:
        latest=sorted(posts,key=lambda x:x["wr_id"],reverse=True)[0]; latest["_seen_key"]=f"etoland:{latest['bo_table']}:{latest['wr_id']}"; to_send=[latest]
    if not to_send: print("[info] no new posts"); return
    sent=[]
    for p in to_send:
        title=p["title"]; url=p["url"]
        m=fetch_content_media_and_summary(url)
        if m.get("title_override"): title=m["title_override"]
        imgs,vids,ifr,summary=m["images"],m["videos"],m["iframes"],m["summary"]
        cap=build_caption(title,url,summary); tg_send_text(cap); time.sleep(1)
        print(f"[debug] media counts wr_id={p['wr_id']}: img={len(imgs)} vid={len(vids)} ifr={len(ifr)}")
        for i in imgs: send_photo_url_or_file(i,None,url); time.sleep(1)
        for v in vids: send_video_url_or_file(v,None,url); time.sleep(1)
        if ifr: tg_send_text("🎥 임베드:\n"+"\n".join(ifr[:5])); time.sleep(1)
        sent.append(p["_seen_key"])
    append_seen(sent); print(f"[info] appended {len(sent)} keys")

if __name__=="__main__":
    process()
