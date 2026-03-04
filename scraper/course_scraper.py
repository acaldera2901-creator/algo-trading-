"""
Course scraper for spacetraders.it
Logs in with provided credentials and extracts all trading course content.
Saves extracted knowledge to knowledge/course_knowledge.json
"""

import json
import os
import time
import logging
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

BASE_URL = "https://spacetraders.it"
COURSE_URL = f"{BASE_URL}/course-category"
OUTPUT_PATH = Path(__file__).parent.parent / "knowledge" / "course_knowledge.json"


def login(page, email: str, password: str) -> bool:
    """Login to the platform and return True if successful."""
    try:
        page.goto(f"{BASE_URL}/login", wait_until="networkidle", timeout=30000)
        page.fill("input[type='email'], input[name='email'], input[name='log']", email)
        page.fill("input[type='password'], input[name='password'], input[name='pwd']", password)
        page.click("button[type='submit'], input[type='submit'], .login-submit, #wp-submit")
        page.wait_for_load_state("networkidle", timeout=15000)

        if "login" in page.url or "logout" not in page.content().lower():
            # Try alternative login paths
            page.goto(f"{BASE_URL}/wp-login.php", wait_until="networkidle", timeout=30000)
            page.fill("#user_login", email)
            page.fill("#user_pass", password)
            page.click("#wp-submit")
            page.wait_for_load_state("networkidle", timeout=15000)

        logger.info(f"Login URL after attempt: {page.url}")
        return True
    except Exception as e:
        logger.error(f"Login failed: {e}")
        return False


def get_course_links(page) -> list[str]:
    """Get all course/lesson links from the course category page."""
    links = []
    try:
        page.goto(COURSE_URL, wait_until="networkidle", timeout=30000)
        time.sleep(2)

        # Extract all course and lesson links
        anchors = page.query_selector_all("a[href*='/course'], a[href*='/lesson'], a[href*='/module'], a.course-link, .course-title a, .ld-course-list-item a")
        seen = set()
        for a in anchors:
            href = a.get_attribute("href")
            if href and href.startswith("http") and href not in seen:
                links.append(href)
                seen.add(href)

        # Also grab any links from the page that look like course content
        all_anchors = page.query_selector_all("a[href]")
        for a in all_anchors:
            href = a.get_attribute("href")
            if href and BASE_URL in href and href not in seen:
                for keyword in ["course", "lesson", "module", "topic", "lezione", "modulo"]:
                    if keyword in href.lower():
                        links.append(href)
                        seen.add(href)
                        break

        logger.info(f"Found {len(links)} course links")
    except Exception as e:
        logger.error(f"Error getting course links: {e}")
    return links


def extract_page_content(page, url: str) -> dict:
    """Extract title and full text content from a course page."""
    result = {"url": url, "title": "", "content": ""}
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
        time.sleep(1)

        # Title
        title_el = page.query_selector("h1, .course-title, .lesson-title, .entry-title")
        if title_el:
            result["title"] = title_el.inner_text().strip()

        # Main content — try common LMS selectors
        content_selectors = [
            ".learndash-wrapper",
            ".course-content",
            ".lesson-content",
            ".entry-content",
            "article",
            "main",
            ".ld-course-step-back",
        ]
        for sel in content_selectors:
            el = page.query_selector(sel)
            if el:
                result["content"] += el.inner_text().strip() + "\n\n"

        if not result["content"]:
            result["content"] = page.inner_text("body")

        result["content"] = result["content"].strip()
        logger.info(f"Extracted: {result['title']} ({len(result['content'])} chars)")
    except PlaywrightTimeout:
        logger.warning(f"Timeout on {url}")
    except Exception as e:
        logger.error(f"Error extracting {url}: {e}")
    return result


def scrape_course(email: str, password: str) -> list[dict]:
    """Main scraping function. Returns list of course page dicts."""
    pages_data = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = context.new_page()

        logged_in = login(page, email, password)
        if not logged_in:
            logger.error("Could not log in. Check credentials.")
            browser.close()
            return []

        course_links = get_course_links(page)

        if not course_links:
            # Fallback: scrape the course-category page itself
            logger.warning("No course links found, scraping main page")
            data = extract_page_content(page, COURSE_URL)
            pages_data.append(data)
        else:
            for url in course_links:
                data = extract_page_content(page, url)
                if data["content"]:
                    pages_data.append(data)
                time.sleep(1)  # polite delay

        browser.close()

    return pages_data


def parse_trading_concepts(pages_data: list[dict]) -> dict:
    """
    Parse raw scraped text into structured trading knowledge.
    Extracts: strategies, entry/exit rules, risk management, concepts.
    """
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
        "entry_rules": ["entrata", "entry", "apri", "open position", "segnale di acquisto", "segnale di vendita", "buy signal", "sell signal"],
        "exit_rules": ["uscita", "exit", "chiudi", "close position", "take profit", "target"],
        "risk_management": ["rischio", "risk", "stop loss", "sl", "drawdown", "position size", "leva", "leverage"],
        "indicators": ["rsi", "macd", "ema", "sma", "bollinger", "atr", "volume", "media mobile", "indicatore"],
        "concepts": ["trend", "supporto", "resistenza", "support", "resistance", "timeframe", "swing", "scalping", "day trading"],
    }

    for page in pages_data:
        text_lower = page["content"].lower()
        lines = page["content"].split("\n")
        for line in lines:
            line = line.strip()
            if not line or len(line) < 10:
                continue
            line_lower = line.lower()
            for category, kws in keywords.items():
                if any(kw in line_lower for kw in kws):
                    if line not in knowledge[category]:
                        knowledge[category].append(line)
                    break

    # Summarize counts
    knowledge["summary"] = {
        cat: len(items)
        for cat, items in knowledge.items()
        if isinstance(items, list)
    }
    return knowledge


def run(email: str, password: str):
    """Entry point: scrape and save knowledge."""
    logger.info("Starting course scraper...")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    pages_data = scrape_course(email, password)
    if not pages_data:
        logger.error("No content scraped. Exiting.")
        return

    knowledge = parse_trading_concepts(pages_data)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(knowledge, f, ensure_ascii=False, indent=2)

    logger.info(f"Knowledge saved to {OUTPUT_PATH}")
    logger.info(f"Summary: {knowledge['summary']}")


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    email = os.getenv("COURSE_EMAIL", "calderaprop@gmail.com")
    password = os.getenv("COURSE_PASSWORD", "")

    if not password:
        print("Set COURSE_PASSWORD in .env file")
        sys.exit(1)

    run(email, password)
