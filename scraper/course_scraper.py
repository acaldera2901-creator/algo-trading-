"""
Course scraper for spacetraders.it
Uses requests + BeautifulSoup (no browser required).
Logs in via WordPress/LearnDash login form, then crawls all course content.
Saves extracted knowledge to knowledge/course_knowledge.json
"""

import json
import logging
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://spacetraders.it"
OUTPUT_PATH = Path(__file__).parent.parent / "knowledge" / "course_knowledge.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8",
}


def _get_soup(session: requests.Session, url: str, retries: int = 3) -> BeautifulSoup:
    for attempt in range(retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            logger.warning(f"GET {url} attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    return BeautifulSoup("", "html.parser")


def login(email: str, password: str) -> requests.Session:
    """Authenticate and return a session with cookies set."""
    session = requests.Session()
    session.headers.update(HEADERS)

    # 1) Load login page to grab any hidden fields / nonce
    login_url = f"{BASE_URL}/wp-login.php"
    soup = _get_soup(session, login_url)

    data = {
        "log": email,
        "pwd": password,
        "wp-submit": "Log In",
        "redirect_to": f"{BASE_URL}/wp-admin/",
        "testcookie": "1",
    }

    # Carry any hidden input fields (nonce, etc.)
    for inp in soup.select("form#loginform input[type=hidden]"):
        name = inp.get("name")
        value = inp.get("value", "")
        if name:
            data[name] = value

    # Set testcookie before POSTing (WordPress requirement)
    session.cookies.set("wordpress_test_cookie", "WP Cookie check", domain=urlparse(BASE_URL).netloc)

    resp = session.post(login_url, data=data, allow_redirects=True, timeout=30)
    logger.info(f"Login response URL: {resp.url}")

    if "wp-admin" in resp.url or "dashboard" in resp.url.lower():
        logger.info("Login successful (redirected to wp-admin)")
        return session

    # Try member area login (some themes use a custom endpoint)
    for login_path in ["/login", "/accedi", "/members", "/my-account"]:
        try:
            url = f"{BASE_URL}{login_path}"
            soup2 = _get_soup(session, url)
            form = soup2.find("form")
            if not form:
                continue
            data2 = {}
            for inp in form.select("input"):
                n, v = inp.get("name"), inp.get("value", "")
                if not n:
                    continue
                if inp.get("type") in ("email", "text") and not data2.get(n):
                    data2[n] = email
                elif inp.get("type") == "password":
                    data2[n] = password
                else:
                    data2[n] = v
            action = form.get("action") or url
            if not action.startswith("http"):
                action = urljoin(BASE_URL, action)
            resp2 = session.post(action, data=data2, allow_redirects=True, timeout=30)
            logger.info(f"Alt login [{login_path}] -> {resp2.url}")
            if login_path.strip("/") not in resp2.url and "login" not in resp2.url.lower():
                logger.info("Login successful via custom form")
                return session
        except Exception as e:
            logger.warning(f"Alt login attempt failed: {e}")

    logger.warning("Login may have failed — proceeding with current session")
    return session


def _is_internal(url: str) -> bool:
    return url.startswith(BASE_URL) or (url.startswith("/") and not url.startswith("//"))


def get_course_links(session: requests.Session) -> list:
    """Crawl course-category and all course/lesson pages for content links."""
    visited = set()
    to_visit = [
        f"{BASE_URL}/course-category",
        f"{BASE_URL}/courses",
        f"{BASE_URL}/corsi",
        BASE_URL,
    ]
    course_links = []

    COURSE_KEYWORDS = ["course", "lesson", "module", "topic", "lezione", "modulo", "corso", "capitolo"]

    while to_visit and len(course_links) < 200:
        url = to_visit.pop(0)
        if url in visited:
            continue
        visited.add(url)

        soup = _get_soup(session, url)
        if not soup.find():
            continue

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href or href.startswith("#") or href.startswith("mailto:"):
                continue
            full = urljoin(BASE_URL, href) if not href.startswith("http") else href
            if not full.startswith(BASE_URL):
                continue
            if full in visited or full in to_visit:
                continue
            path_lower = urlparse(full).path.lower()
            if any(kw in path_lower for kw in COURSE_KEYWORDS):
                if full not in course_links:
                    course_links.append(full)
                to_visit.append(full)
            elif full not in to_visit:
                to_visit.append(full)

        time.sleep(0.5)

    logger.info(f"Found {len(course_links)} course-related URLs")
    return course_links or [f"{BASE_URL}/course-category"]


def extract_page_content(session: requests.Session, url: str) -> dict:
    """Extract structured content from a single course page."""
    result = {"url": url, "title": "", "content": ""}
    soup = _get_soup(session, url)
    if not soup.find():
        return result

    # Title
    for sel in ["h1.entry-title", "h1.course-title", "h1.lesson-title", "h1", ".page-title"]:
        el = soup.select_one(sel)
        if el:
            result["title"] = el.get_text(strip=True)
            break

    # Main content — try common LMS/theme selectors
    texts = []
    for sel in [
        ".learndash-wrapper", ".ld-course-step-back",
        ".course-content", ".lesson-content",
        ".entry-content", "article .content",
        "article", "main .content", "main",
        "#content", ".post-content",
    ]:
        els = soup.select(sel)
        for el in els:
            for noise in el.select("nav, .sidebar, header, footer, .widget, script, style"):
                noise.decompose()
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 100:
                texts.append(text)
                break
        if texts:
            break

    result["content"] = "\n\n".join(texts).strip()

    if not result["content"]:
        body = soup.find("body")
        if body:
            for tag in body.select("nav, header, footer, script, style, .sidebar"):
                tag.decompose()
            result["content"] = body.get_text(separator="\n", strip=True)

    logger.info(f"  [{result['title'] or url}] — {len(result['content'])} chars")
    return result


def parse_trading_concepts(pages_data: list) -> dict:
    """Parse raw scraped text into structured trading knowledge."""
    knowledge = {
        "raw_pages": pages_data,
        "strategies": [],
        "entry_rules": [],
        "exit_rules": [],
        "risk_management": [],
        "concepts": [],
        "indicators": [],
    }

    keywords = {
        "strategies": ["strategia", "strategy", "setup", "pattern"],
        "entry_rules": [
            "entrata", "entry", "apri", "open position",
            "segnale di acquisto", "segnale di vendita", "buy signal", "sell signal",
        ],
        "exit_rules": ["uscita", "exit", "chiudi", "close position", "take profit", "target"],
        "risk_management": [
            "rischio", "risk", "stop loss", "sl", "drawdown",
            "position size", "leva", "leverage", "gestione",
        ],
        "indicators": [
            "rsi", "macd", "ema", "sma", "bollinger", "atr",
            "volume", "media mobile", "indicatore",
        ],
        "concepts": [
            "trend", "supporto", "resistenza", "support", "resistance",
            "timeframe", "swing", "scalping", "day trading", "fibonacci",
        ],
    }

    for page in pages_data:
        for line in page["content"].split("\n"):
            line = line.strip()
            if not line or len(line) < 15:
                continue
            line_lower = line.lower()
            for category, kws in keywords.items():
                if any(kw in line_lower for kw in kws):
                    lst = knowledge[category]
                    if line not in lst:
                        lst.append(line)
                    break

    knowledge["summary"] = {
        cat: len(items)
        for cat, items in knowledge.items()
        if isinstance(items, list) and cat != "raw_pages"
    }
    return knowledge


def run(email: str, password: str):
    """Main entry: login -> scrape -> save knowledge."""
    logger.info("Starting course scraper (requests mode)...")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    session = login(email, password)
    links = get_course_links(session)

    pages_data = []
    for url in links:
        data = extract_page_content(session, url)
        if data["content"]:
            pages_data.append(data)
        time.sleep(0.8)

    if not pages_data:
        logger.error("No content extracted.")
        return

    knowledge = parse_trading_concepts(pages_data)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(knowledge, f, ensure_ascii=False, indent=2)

    logger.info(f"Saved {len(pages_data)} pages -> {OUTPUT_PATH}")
    logger.info(f"Summary: {knowledge['summary']}")


if __name__ == "__main__":
    import os
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    email = os.getenv("COURSE_EMAIL", "calderaprop@gmail.com")
    password = os.getenv("COURSE_PASSWORD", "")
    if not password:
        print("Set COURSE_PASSWORD in .env")
        sys.exit(1)
    run(email, password)
