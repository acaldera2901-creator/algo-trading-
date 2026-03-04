"""
Entry point for the Forex Trading Agent.

Usage:
    # First-time setup: scrape the trading course
    python main.py --scrape

    # Run the agent (dry-run by default via .env)
    python main.py

    # Single analysis pass (no continuous loop)
    python main.py --once
"""

import argparse
import logging
import os
import sys

import config

# ─── Logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("agent.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def scrape_course():
    """Run the course scraper to build the knowledge base."""
    email = os.getenv("COURSE_EMAIL", "calderaprop@gmail.com")
    password = os.getenv("COURSE_PASSWORD", "")
    if not password:
        logger.error("Set COURSE_PASSWORD in your .env file before scraping.")
        sys.exit(1)

    from scraper.course_scraper import run
    run(email, password)
    logger.info("Course scraping complete. knowledge/course_knowledge.json updated.")


def main():
    parser = argparse.ArgumentParser(description="Forex Trading Agent")
    parser.add_argument("--scrape", action="store_true", help="Scrape the trading course first")
    parser.add_argument("--once", action="store_true", help="Run a single analysis pass and exit")
    args = parser.parse_args()

    if args.scrape:
        scrape_course()

    if not config.COURSE_KNOWLEDGE_PATH.exists() and not args.scrape:
        logger.warning(
            "No course knowledge found. Run with --scrape first: python main.py --scrape"
        )

    logger.info("=" * 60)
    logger.info("  Forex Trading Agent — starting up")
    logger.info(f"  Broker: {config.BROKER.upper()} | DRY RUN: {config.DRY_RUN}")
    logger.info(f"  Symbols: {config.SYMBOLS}")
    logger.info(f"  Timeframe: {config.TIMEFRAME}")
    logger.info("=" * 60)

    from agent.core import TradingAgent
    agent = TradingAgent()

    if args.once:
        agent.run_once()
    else:
        agent.run_loop()


if __name__ == "__main__":
    main()
