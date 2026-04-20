"""
main.py — Apify Actor entry point for Coursology Q-Bank Scraper.

Flow:
  1. Read input from Apify (email, password, test_url, options)
  2. Launch Playwright Chromium
  3. Log in to Coursology
  4. Navigate to the test URL
  5. Run scraper loop → push each question to Dataset
  6. Save audio to Key-Value Store
"""
from __future__ import annotations

import asyncio
import sys
import os

from apify import Actor

# Make src/ importable when running locally
sys.path.insert(0, os.path.dirname(__file__))

from browser import launch_browser, login
from scraper import scrape
from storage import load_state


async def main() -> None:
    async with Actor:
        # ── 1. Read input ────────────────────────────────────────────────────
        inp = await Actor.get_input() or {}

        email         = inp.get("email", "")
        password      = inp.get("password", "")
        test_url      = inp.get("test_url", "")
        max_questions = int(inp.get("max_questions") or 0)
        start_from    = int(inp.get("start_from") or 1)
        delay_min_ms  = int(inp.get("delay_min_ms") or 1200)
        delay_max_ms  = int(inp.get("delay_max_ms") or 2500)
        save_audio    = bool(inp.get("save_audio", True))
        headless      = bool(inp.get("headless", True))

        if not email or not password or not test_url:
            await Actor.fail(
                status_message="Missing required input: email, password, and test_url are all required."
            )
            return

        # ── 2. Auto-resume: load last saved question number ──────────────────
        if start_from == 1:
            saved_n = await load_state()
            if saved_n > 0:
                print(f"[*] Found saved state — resuming from question {saved_n + 1}.")
                start_from = saved_n + 1

        # ── 3. Launch browser ────────────────────────────────────────────────
        print("[*] Launching browser…")
        browser, context, page = await launch_browser(headless=headless)

        try:
            # ── 4. Log in ────────────────────────────────────────────────────
            await login(page, email, password)

            # ── 5. Navigate to test page ─────────────────────────────────────
            print(f"[*] Navigating to test URL: {test_url}")
            await page.goto(test_url, wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(2.0)

            # ── 6. Scrape ────────────────────────────────────────────────────
            await scrape(
                page=page,
                max_questions=max_questions,
                start_from=start_from,
                delay_min_ms=delay_min_ms,
                delay_max_ms=delay_max_ms,
                save_audio_files=save_audio,
            )

        except Exception as e:
            print(f"\n[ERROR] Actor failed: {e}")
            await Actor.fail(status_message=str(e))
            raise

        finally:
            await context.close()
            await browser.close()

        print("\n[✓] Actor finished successfully.")


if __name__ == "__main__":
    asyncio.run(main())
