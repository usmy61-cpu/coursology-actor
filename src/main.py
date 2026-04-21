"""
main.py — Apify Actor entry point for Coursology Q-Bank Scraper.
"""
from __future__ import annotations

import asyncio
import sys
import os

from apify import Actor

_src_dir = os.path.dirname(os.path.abspath(__file__))
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from browser import launch_browser, login
from scraper import scrape
from storage import load_state


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}

        email           = inp.get("email", "")
        password        = inp.get("password", "")
        test_url        = inp.get("test_url", "")
        output_filename = inp.get("output_filename", "questions").strip() or "questions"
        max_questions   = int(inp.get("max_questions") or 0)
        start_from      = int(inp.get("start_from") or 1)
        delay_min_ms    = int(inp.get("delay_min_ms") or 1200)
        delay_max_ms    = int(inp.get("delay_max_ms") or 2500)
        save_audio      = bool(inp.get("save_audio", True))
        headless        = bool(inp.get("headless", True))

        if not email or not password or not test_url:
            await Actor.fail(
                status_message="Missing required input: email, password, and test_url are all required."
            )
            return

        safe_filename = output_filename.replace(" ", "_")
        print(f"[*] Output will be saved as: {safe_filename}.json")

        if start_from == 1:
            saved_n = await load_state()
            if saved_n > 0:
                print(f"[*] Found saved state — resuming from question {saved_n + 1}.")
                start_from = saved_n + 1

        print("[*] Launching browser…")
        browser, context, page = await launch_browser(headless=headless)

        try:
            await login(page, email, password)

            print(f"[*] Navigating to test URL: {test_url}")
            await page.goto(test_url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(2.0)

            await scrape(
                page=page,
                max_questions=max_questions,
                start_from=start_from,
                delay_min_ms=delay_min_ms,
                delay_max_ms=delay_max_ms,
                save_audio_files=save_audio,
                output_filename=safe_filename,
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
