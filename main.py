import requests
from bs4 import BeautifulSoup
import time
import os

# --- 설정 부분 ---
TELEGRAM_BOT_TOKEN = '8247528958:AAERW0gt7fZYb7ZYBKRzEUeIR3W1AK6coCk'
TELEGRAM_CHAT_ID = '6137638808'
TARGET_URL = 'https://www.etoland.co.kr/bbs/hgall.php?bo_table=etohumor07&sca=%BE%E0%C8%C4'

# 중요: 게시물 링크를 가리키는 정확한 CSS 선택자로 바꿔야 합니다.
# 아래는 'sbj'라는 클래스를 가진 td 태그 안의 a 태그를 선택하는 예시입니다.
# 페이지 검사를 통해 직접 확인하고 수정해야 합니다.
POST_LINK_SELECTOR = 'td.sbj a'

# 마지막으로 확인한 게시물 링크를 저장할 파일 이름
LAST_POST_FILE = 'last_post.txt'

def send_telegram_message(message):
    """지정한 텔레그램 채팅으로 메시지를 보냅니다."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status()  # 요청이 실패하면 에러를 발생시킴
        print("텔레그램 알림을 성공적으로 보냈습니다!")
    except requests.exceptions.RequestException as e:
        print(f"텔레그램 메시지 전송 중 에러 발생: {e}")

def get_last_post():
    """파일에서 마지막으로 확인한 게시물의 URL을 읽어옵니다."""
    if not os.path.exists(LAST_POST_FILE):
        return None
    with open(LAST_POST_FILE, 'r') as f:
        return f.read().strip()

def save_last_post(url):
    """가장 최신 게시물의 URL을 파일에 저장합니다."""
    with open(LAST_POST_FILE, 'w') as f:
        f.write(url)

def check_for_new_posts():
    """웹사이트를 크롤링하여 새 글이 있는지 확인합니다."""
    print("새로운 게시물을 확인하는 중...")
    try:
        headers = {'User-Agent': 'Mozilla/5.0'} # 봇으로 인식되지 않기 위한 헤더
        response = requests.get(TARGET_URL, headers=headers)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')
        # 지정한 CSS 선택자를 사용해 모든 게시물 링크를 찾음
        post_links = soup.select(POST_LINK_SELECTOR)

        if not post_links:
            print("게시물을 찾지 못했습니다. CSS 선택자가 올바른지 확인하세요.")
            return

        # 가장 최신 글 (보통 목록의 첫 번째)
        latest_post_element = post_links[0]
        latest_post_title = latest_post_element.get_text(strip=True)
        latest_post_url = latest_post_element['href']

        # URL이 상대 경로일 경우, 전체 주소로 만들어 줌
        if not latest_post_url.startswith('http'):
            from urllib.parse import urljoin
            latest_post_url = urljoin(TARGET_URL, latest_post_url)

        last_processed_post = get_last_post()

        if latest_post_url != last_processed_post:
            print(f"새 글 발견: {latest_post_title}")
            message = f"<b>이토랜드 유머게시판 새 글:</b>\n\n<a href='{latest_post_url}'>{latest_post_title}</a>"
            send_telegram_message(message)
            save_last_post(latest_post_url)
        else:
            print("지난번 확인 이후 새 글이 없습니다.")

    except requests.exceptions.RequestException as e:
        print(f"웹사이트를 가져오는 중 에러 발생: {e}")
    except Exception as e:
        print(f"예상치 못한 에러 발생: {e}")

if __name__ == '__main__':
    while True:
        check_for_new_posts()
        # 10분(600초)마다 한 번씩 확인
        print("10분 대기...")
        time.sleep(600)