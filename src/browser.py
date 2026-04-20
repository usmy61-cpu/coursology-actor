"""
browser.py — Browser lifecycle and authentication
"""
from __future__ import annotations

import asyncio
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from apify import Actor

LOGIN_URL            = "https://coursology-qbank.com/auth/signin"
LOGIN_PATH_FRAGMENTS = ["/auth/signin", "/auth/login", "/login", "/signin"]


async def _kv_save_png(page: Page, name: str) -> None:
    try:
        png = await page.screenshot(full_page=True)
        store = await Actor.open_key_value_store()
        await store.set_value(name, png, content_type="image/png")
        print(f"[debug] Screenshot → KV:{name}")
    except Exception as e:
        print(f"[debug] screenshot error: {e}")


async def _kv_save_html(html: str, name: str) -> None:
    try:
        store = await Actor.open_key_value_store()
        await store.set_value(name, html.encode("utf-8"), content_type="text/html; charset=utf-8")
        print(f"[debug] HTML dump → KV:{name}")
    except Exception as e:
        print(f"[debug] html dump error: {e}")


async def _fill_field(page: Page, selector: str, value: str) -> None:
    """Click, clear, and type into a field — works with React and plain HTML."""
    loc = page.locator(selector).first
    await loc.wait_for(state="visible", timeout=10_000)
    await loc.click()
    await asyncio.sleep(0.15)
    await loc.triple_click()
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Delete")
    await asyncio.sleep(0.1)
    await loc.type(value, delay=45)
    await asyncio.sleep(0.2)


async def login(page: Page, email: str, password: str) -> None:
    print(f"[*] Navigating to login page: {LOGIN_URL}")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await asyncio.sleep(2.5)

    await _kv_save_png(page, "debug_login_page.png")

    # Wait for the username field (confirmed id from debug_dom_dump.txt)
    try:
        await page.wait_for_selector("#username-input", timeout=10_000)
    except Exception:
        await _kv_save_html(await page.content(), "debug_no_form.html")
        raise RuntimeError(
            "Login form not found — #username-input missing after 10s.\n"
            "Check KV Store: debug_login_page.png and debug_no_form.html"
        )

    # Fill username/email — field id is 'username-input', type=text
    print("[*] Filling username field...")
    await _fill_field(page, "#username-input", email)

    # Fill password — field id is 'password-input'
    print("[*] Filling password field...")
    await _fill_field(page, "#password-input", password)

    await _kv_save_png(page, "debug_filled_form.png")

    # Click the submit button (type=submit, text="Sign in")
    print("[*] Clicking Sign in...")
    await page.locator('button[type="submit"]').first.click()

    # Wait for redirect away from signin page
    try:
        await page.wait_for_url(
            lambda url: not any(frag in url for frag in LOGIN_PATH_FRAGMENTS),
            timeout=25_000,
        )
        print(f"[+] Login successful! URL: {page.url}")
        await _kv_save_png(page, "debug_post_login.png")
    except Exception:
        current = page.url
        await _kv_save_png(page, "debug_login_failed.png")
        await _kv_save_html(await page.content(), "debug_login_failed.html")
        raise RuntimeError(
            f"Login failed — still on '{current}' after submitting.\n"
            "  → debug_filled_form.png : were both fields filled?\n"
            "  → debug_login_failed.png: is there an error message on screen?\n"
            "  → Verify email/password are correct."
        )


async def launch_browser(headless: bool = True) -> tuple[Browser, BrowserContext, Page]:
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

    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
    """)

    page = await context.new_page()
    return browser, context, page
