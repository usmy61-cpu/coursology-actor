"""
browser.py — Browser lifecycle and authentication
"""
from __future__ import annotations

import asyncio
import base64
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from apify import Actor


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


async def _save_screenshot(page: Page, name: str) -> None:
    """Save a screenshot to the Apify KV Store for debugging."""
    try:
        png = await page.screenshot(full_page=True)
        store = await Actor.open_key_value_store()
        await store.set_value(name, png, content_type="image/png")
        print(f"[debug] Screenshot saved to KV Store → {name}")
    except Exception as e:
        print(f"[debug] Could not save screenshot: {e}")


async def _dump_inputs(page: Page) -> None:
    """Print all input fields found on the page for debugging."""
    try:
        inputs = await page.evaluate("""
            () => Array.from(document.querySelectorAll('input')).map(i => ({
                tag: i.tagName,
                type: i.type,
                name: i.name,
                id: i.id,
                placeholder: i.placeholder,
                className: i.className.substring(0, 80),
                visible: i.offsetWidth > 0 && i.offsetHeight > 0
            }))
        """)
        print(f"[debug] Found {len(inputs)} input(s) on page:")
        for inp in inputs:
            print(f"  {inp}")
    except Exception as e:
        print(f"[debug] _dump_inputs error: {e}")


async def _fill_react_input(page: Page, selector: str, value: str) -> bool:
    """
    Fill a React-controlled input field.
    React intercepts native events, so a plain fill() sometimes doesn't
    trigger onChange. This method:
      1. Clicks to focus
      2. Clears with triple-click + Delete
      3. Types character by character (triggers React synthetic events)
    Returns True if the field was found and filled.
    """
    try:
        el = page.locator(selector).first
        await el.wait_for(state="visible", timeout=8_000)
        await el.click()
        await asyncio.sleep(0.2)
        # Clear existing value
        await el.triple_click()
        await page.keyboard.press("Delete")
        await asyncio.sleep(0.1)
        # Type slowly so React's onChange fires on every keystroke
        await el.type(value, delay=40)
        await asyncio.sleep(0.2)
        return True
    except Exception:
        return False


async def login(page: Page, email: str, password: str) -> None:
    """
    Log in to Coursology.

    Strategy:
      1. Navigate to /login and wait for the form to be interactive.
      2. Dump all inputs to logs for debugging.
      3. Try multiple selector patterns for email + password fields using
         React-aware typing (not just fill).
      4. Click the submit button.
      5. Wait up to 20s for URL to change away from /login.
      6. On failure: save a screenshot + page HTML to KV Store for diagnosis.
    """
    print("[*] Navigating to login page…")
    await page.goto("https://coursology.com/login", wait_until="domcontentloaded")

    # Give React time to mount the form
    await asyncio.sleep(2.0)

    # Wait for at least one input to be visible
    try:
        await page.wait_for_selector("input", timeout=10_000)
    except Exception:
        print("[!] No <input> found after 10s — saving screenshot for diagnosis.")
        await _save_screenshot(page, "debug_no_inputs.png")

    # Dump inputs to help diagnose selector issues
    await _dump_inputs(page)
    await _save_screenshot(page, "debug_login_page.png")

    # ── Fill email ──────────────────────────────────────────────────────────
    email_selectors = [
        'input[name="email"]',
        'input[type="email"]',
        'input[placeholder*="email" i]',
        'input[placeholder*="Email" i]',
        'input[id*="email" i]',
        'input[autocomplete="email"]',
        'input[autocomplete="username"]',
        'form input:first-of-type',   # first input in a form
        'input:not([type="password"]):not([type="hidden"]):not([type="checkbox"])',
    ]
    email_filled = False
    for sel in email_selectors:
        if await _fill_react_input(page, sel, email):
            print(f"[+] Email filled via selector: {sel}")
            email_filled = True
            break
    if not email_filled:
        print("[!] WARNING: Could not find email field — login may fail.")

    await asyncio.sleep(0.3)

    # ── Fill password ───────────────────────────────────────────────────────
    password_selectors = [
        'input[type="password"]',
        'input[name="password"]',
        'input[placeholder*="password" i]',
        'input[id*="password" i]',
        'input[autocomplete="current-password"]',
    ]
    password_filled = False
    for sel in password_selectors:
        if await _fill_react_input(page, sel, password):
            print(f"[+] Password filled via selector: {sel}")
            password_filled = True
            break
    if not password_filled:
        print("[!] WARNING: Could not find password field — login may fail.")

    await asyncio.sleep(0.3)
    await _save_screenshot(page, "debug_filled_form.png")

    # ── Submit ──────────────────────────────────────────────────────────────
    submit_selectors = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Log in")',
        'button:has-text("Login")',
        'button:has-text("Sign in")',
        'button:has-text("Continue")',
        'form button',          # any button inside a form
    ]
    submitted = False
    for sel in submit_selectors:
        try:
            btn = page.locator(sel).first
            await btn.wait_for(state="visible", timeout=3_000)
            await btn.click()
            print(f"[+] Clicked submit via: {sel}")
            submitted = True
            break
        except Exception:
            continue

    if not submitted:
        # Last resort: press Enter on the password field
        print("[!] No submit button found — pressing Enter on password field.")
        try:
            await page.keyboard.press("Enter")
            submitted = True
        except Exception:
            pass

    # ── Wait for successful login ────────────────────────────────────────────
    try:
        await page.wait_for_url(lambda url: "/login" not in url, timeout=20_000)
        print(f"[+] Logged in! Current URL: {page.url}")
        await _save_screenshot(page, "debug_post_login.png")
    except Exception:
        current = page.url
        # Save diagnostic artifacts before raising
        await _save_screenshot(page, "debug_login_failed.png")
        try:
            html = await page.content()
            store = await Actor.open_key_value_store()
            await store.set_value(
                "debug_login_failed.html",
                html.encode("utf-8"),
                content_type="text/html; charset=utf-8",
            )
            print("[debug] Login failure HTML saved to KV Store → debug_login_failed.html")
        except Exception as he:
            print(f"[debug] Could not save HTML: {he}")

        if "/login" in current:
            raise RuntimeError(
                f"Login failed — still on {current}.\n"
                "  → Check KV Store for 'debug_login_failed.png' and 'debug_login_failed.html'\n"
                "    to see exactly what the browser saw.\n"
                "  → Verify your email/password are correct.\n"
                "  → The login selectors may need updating if Coursology changed their UI."
            )
        print(f"[+] Login appears successful. URL: {current}")
