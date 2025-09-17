import os
import requests
from bs4 import BeautifulSoup
import sqlite3
import time

# === 환경 변수 (Railway에서 설정) ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")   # 텔레그램 봇 토큰
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # 메시지를 받을 채팅 ID (개인 or 그룹)
BOARD_URL = "https://www.etoland.co.kr/bbs/hgall.php?bo_table=etohumor07&sca=%BE%E0%C8%C4"

# === DB (sqlite로 최신 글 추적) ===
DB_PATH = "posts.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS posts (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

def already_sent(post_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id FROM posts WHERE id = ?", (post_id,))
    result = cur.fetchone()
    conn.close()
    return result is not None

def save_post(post_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO posts (id) VALUES (?)", (post_id,))
    conn.commit()
    conn.close()

# === 텔레그램 메시지 전송 ===
def send_telegram(text, photo=None):
    if photo:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": text}
        files = {"photo": requests.get(photo).content}
        requests.post(url, data=payload, files={"photo": ("image.jpg", files["photo"])})
    else:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
        requests.post(url, data=payload)

# === 크롤링 ===
def crawl():
    res = requests.get(BOARD_URL, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(res.text, "html.parser")

    rows = soup.select("div.etl_board li")  # 실제 DOM 구조 맞춰서 수정 필요
    for row in rows[:5]:  # 최신 5개만 확인
        link_tag = row.select_one("a")
        if not link_tag:
            continue

        href = link_tag["href"]
        post_id = href.split("wr_id=")[-1]
        title = link_tag.get_text(strip=True)

        if already_sent(post_id):
            continue

        # 본문 크롤링
        post_res = requests.get(href, headers={"User-Agent": "Mozilla/5.0"})
        post_soup = BeautifulSoup(post_res.text, "html.parser")
        img_tag = post_soup.select_one("div.view_content img")

        # 텔레그램 전송
        if img_tag:
            img_url = img_tag["src"]
            send_telegram(f"📌 {title}\n{href}", photo=img_url)
        else:
            send_telegram(f"📌 {title}\n{href}")

        save_post(post_id)
        time.sleep(1)

if __name__ == "__main__":
    init_db()
    crawl()
