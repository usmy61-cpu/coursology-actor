"""
browser.py — Browser lifecycle and authentication
"""
from __future__ import annotations

import asyncio
from playwright.async_api import async_playwright, Browser, BrowserContext, Page


async def launch_browser(headless: bool = True) -> tuple[Browser, BrowserContext, Page]:
    """
    Launch a Playwright Chromium instance with stealth-friendly settings.
    Returns (browser, context, page).
    """
    pw = await async_playwright().start()

    browser = await pw.chromium.launch(
        headless=headless,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )

    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="America/New_York",
        java_script_enabled=True,
    )

    # Hide navigator.webdriver property — basic stealth
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
    """)

    page = await context.new_page()
    return browser, context, page


async def login(page: Page, email: str, password: str) -> None:
    """
    Log in to Coursology.  Tries the most common selector patterns and waits
    for a successful redirect away from /login.
    """
    print("[*] Navigating to login page…")
    await page.goto("https://coursology.com/login", wait_until="networkidle")
    await asyncio.sleep(1.0)

    # ── Fill email ──────────────────────────────────────────────────────────
    for sel in ['input[name="email"]', 'input[type="email"]', '#email']:
        try:
            await page.fill(sel, email, timeout=4_000)
            break
        except Exception:
            continue

    # ── Fill password ───────────────────────────────────────────────────────
    for sel in ['input[name="password"]', 'input[type="password"]', '#password']:
        try:
            await page.fill(sel, password, timeout=4_000)
            break
        except Exception:
            continue

    # ── Submit ──────────────────────────────────────────────────────────────
    for sel in [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Log in")',
        'button:has-text("Sign in")',
        'button:has-text("Login")',
    ]:
        try:
            await page.click(sel, timeout=4_000)
            break
        except Exception:
            continue

    # ── Wait for successful login (URL changes away from /login) ────────────
    try:
        await page.wait_for_url(lambda url: "/login" not in url, timeout=15_000)
        print(f"[+] Logged in! Current URL: {page.url}")
    except Exception:
        current = page.url
        if "/login" in current:
            raise RuntimeError(
                f"Login failed — still on {current}. "
                "Check your email/password or whether Coursology changed its UI."
            )
        print(f"[+] Login appears successful. URL: {current}")
