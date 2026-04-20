"""
browser.py — Browser lifecycle and authentication

Handles Coursology's login at https://coursology-qbank.com/auth/signin.
The form may be rendered:
  (a) directly in the main document, or
  (b) inside a Clerk.js / NextAuth iframe (common for Next.js apps).

Strategy: try the main frame first, then search all child iframes.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from playwright.async_api import (
    async_playwright, Browser, BrowserContext, Page, Frame
)
from apify import Actor

# ── Correct login URL (from actor logs) ───────────────────────────────────────
LOGIN_URL  = "https://coursology-qbank.com/auth/signin"
# URL pattern that indicates a successful login (anything that is NOT signin/login)
LOGIN_PATH_FRAGMENTS = ["/auth/signin", "/auth/login", "/login", "/signin"]


# ── KV Store helpers ──────────────────────────────────────────────────────────

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


async def _kv_save_text(text: str, name: str) -> None:
    try:
        store = await Actor.open_key_value_store()
        await store.set_value(name, text.encode("utf-8"), content_type="text/plain; charset=utf-8")
        print(f"[debug] Text dump → KV:{name}")
    except Exception as e:
        print(f"[debug] text dump error: {e}")


# ── Deep DOM dump (main frame + all iframes) ──────────────────────────────────

async def _dump_dom(page: Page) -> None:
    """
    Save a combined diagnostic text file listing:
    - All inputs in the main frame
    - All iframe URLs
    - All inputs inside each iframe (if same-origin / accessible)
    """
    lines: list[str] = []

    async def _dump_frame(frame: Frame, label: str) -> None:
        lines.append(f"\n=== {label} (url={frame.url}) ===")
        try:
            inputs = await frame.evaluate("""
                () => Array.from(document.querySelectorAll('input')).map(i => ({
                    type: i.type, name: i.name, id: i.id,
                    placeholder: i.placeholder,
                    autocomplete: i.autocomplete,
                    visible: i.offsetWidth > 0 && i.offsetHeight > 0
                }))
            """)
            lines.append(f"  inputs ({len(inputs)}):")
            for inp in inputs:
                lines.append(f"    {inp}")
            btns = await frame.evaluate("""
                () => Array.from(document.querySelectorAll('button')).map(b => ({
                    type: b.type, text: b.innerText.trim().substring(0,60),
                    visible: b.offsetWidth > 0 && b.offsetHeight > 0
                }))
            """)
            lines.append(f"  buttons ({len(btns)}):")
            for b in btns:
                lines.append(f"    {b}")
        except Exception as e:
            lines.append(f"  [could not evaluate: {e}]")

    await _dump_frame(page.main_frame, "MAIN")
    for i, frame in enumerate(page.frames):
        if frame != page.main_frame:
            await _dump_frame(frame, f"IFRAME[{i}]")

    await _kv_save_text("\n".join(lines), "debug_dom_dump.txt")


# ── React-aware field fill (works in any Frame) ───────────────────────────────

async def _fill_in_frame(frame: Frame, selector: str, value: str) -> bool:
    """
    Fill a field inside a given frame using React-compatible typing.
    Returns True on success.
    """
    try:
        loc = frame.locator(selector).first
        await loc.wait_for(state="visible", timeout=5_000)
        await loc.click()
        await asyncio.sleep(0.15)
        await loc.triple_click()
        await frame.keyboard.press("Control+a")
        await frame.keyboard.press("Delete")
        await asyncio.sleep(0.1)
        await loc.type(value, delay=45)
        await asyncio.sleep(0.2)
        return True
    except Exception:
        return False


async def _click_in_frame(frame: Frame, selectors: list[str]) -> bool:
    """Try each selector in order and click the first visible one. Returns True on success."""
    for sel in selectors:
        try:
            btn = frame.locator(sel).first
            await btn.wait_for(state="visible", timeout=3_000)
            await btn.click()
            print(f"[+] Clicked submit via: {sel}")
            return True
        except Exception:
            continue
    return False


# ── Core login logic ──────────────────────────────────────────────────────────

EMAIL_SELECTORS = [
    'input[name="email"]',
    'input[name="identifier"]',       # Clerk.js uses "identifier"
    'input[name="emailAddress"]',
    'input[type="email"]',
    'input[autocomplete="email"]',
    'input[autocomplete="username"]',
    'input[placeholder*="email" i]',
    'input[placeholder*="Email" i]',
    'input[id*="email" i]',
    'input[id*="identifier" i]',
    # last resort: first non-password visible input
    'input:not([type="password"]):not([type="hidden"]):not([type="checkbox"]):not([type="radio"])',
]

PASSWORD_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]',
    'input[autocomplete="current-password"]',
    'input[placeholder*="password" i]',
    'input[id*="password" i]',
]

SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Continue")',
    'button:has-text("Sign in")',
    'button:has-text("Log in")',
    'button:has-text("Login")',
    'button:has-text("Sign In")',
    'form button',
]


async def _try_login_in_frame(frame: Frame, email: str, password: str) -> bool:
    """
    Attempt to fill email + password + click submit inside a single frame.
    Returns True if the email field was successfully found and filled
    (doesn't guarantee login succeeded — caller checks URL afterwards).
    """
    # ── Email ──────────────────────────────────────────────────────────────
    email_filled = False

    # Clerk multi-step: email first, then "Continue", then password
    for sel in EMAIL_SELECTORS:
        if await _fill_in_frame(frame, sel, email):
            print(f"[+] Email filled in frame '{frame.url[:60]}' via: {sel}")
            email_filled = True
            break

    if not email_filled:
        return False

    # Some forms (Clerk) show password only after clicking "Continue"
    # Try clicking Continue/Next, wait briefly, then check for password
    await asyncio.sleep(0.4)
    has_password = False
    for sel in PASSWORD_SELECTORS:
        try:
            loc = frame.locator(sel).first
            await loc.wait_for(state="visible", timeout=1_500)
            has_password = True
            break
        except Exception:
            pass

    if not has_password:
        # Click a "Continue" / "Next" button if present (Clerk step 1)
        for sel in ['button:has-text("Continue")', 'button[type="submit"]', 'button:has-text("Next")']:
            try:
                btn = frame.locator(sel).first
                await btn.wait_for(state="visible", timeout=2_000)
                await btn.click()
                print(f"[+] Clicked intermediate button: {sel}")
                await asyncio.sleep(1.5)
                break
            except Exception:
                continue

    # ── Password ────────────────────────────────────────────────────────────
    password_filled = False
    for sel in PASSWORD_SELECTORS:
        if await _fill_in_frame(frame, sel, password):
            print(f"[+] Password filled via: {sel}")
            password_filled = True
            break

    if not password_filled:
        print("[!] Password field not found in this frame — will still try to submit.")

    await asyncio.sleep(0.3)

    # ── Submit ──────────────────────────────────────────────────────────────
    submitted = await _click_in_frame(frame, SUBMIT_SELECTORS)
    if not submitted:
        print("[!] No submit button found — pressing Enter.")
        await frame.keyboard.press("Enter")

    return True


async def login(page: Page, email: str, password: str) -> None:
    """
    Log in to Coursology at coursology-qbank.com/auth/signin.

    Handles both direct forms and Clerk.js/NextAuth iframes.
    Saves diagnostic screenshots + DOM dump to KV Store on every run.
    Raises RuntimeError (with KV Store instructions) if login fails.
    """
    print(f"[*] Navigating to login page: {LOGIN_URL}")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await asyncio.sleep(2.5)   # let JS frameworks mount

    # Save what we see immediately
    await _kv_save_png(page, "debug_login_page.png")

    # ── Wait for any input to appear (main frame or any iframe) ───────────
    input_appeared = False
    for _ in range(12):   # poll for up to 6 seconds
        for frame in page.frames:
            try:
                inp = await frame.query_selector("input:not([type='hidden'])")
                if inp:
                    input_appeared = True
                    break
            except Exception:
                pass
        if input_appeared:
            break
        await asyncio.sleep(0.5)

    # Always dump the full DOM for diagnosis before proceeding
    await _dump_dom(page)

    if not input_appeared:
        await _kv_save_html(await page.content(), "debug_no_inputs.html")
        raise RuntimeError(
            "No visible <input> found on the login page after 6 seconds.\n"
            "  → Check KV Store: 'debug_login_page.png' and 'debug_no_inputs.html'\n"
            "  → The page may require JavaScript that isn't loading, or the URL is wrong."
        )

    # ── Try main frame first, then each iframe ─────────────────────────────
    frames_to_try: list[Frame] = [page.main_frame] + [
        f for f in page.frames if f != page.main_frame
    ]

    login_attempted = False
    for frame in frames_to_try:
        try:
            attempted = await _try_login_in_frame(frame, email, password)
            if attempted:
                login_attempted = True
                break
        except Exception as e:
            print(f"[debug] Frame {frame.url[:50]} error: {e}")
            continue

    if not login_attempted:
        await _kv_save_png(page, "debug_login_failed.png")
        await _kv_save_html(await page.content(), "debug_login_failed.html")
        raise RuntimeError(
            "Could not locate the username/email input on the login page.\n"
            "  → Check KV Store for 'debug_dom_dump.txt' — it lists every input\n"
            "    found in the main frame and all iframes.\n"
            "  → Use the selector information there to update EMAIL_SELECTORS in browser.py."
        )

    # ── Wait for URL to leave the auth/signin page ─────────────────────────
    await _kv_save_png(page, "debug_filled_form.png")
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
        await _dump_dom(page)

        raise RuntimeError(
            f"Login failed — still on '{current}' after submitting.\n"
            "  → Check KV Store artifacts:\n"
            "      debug_filled_form.png  — were the fields filled correctly?\n"
            "      debug_login_failed.png — what error message is shown?\n"
            "      debug_login_failed.html — raw page source\n"
            "      debug_dom_dump.txt     — all inputs/buttons found\n"
            "  → If fields look empty: the site may block automated input.\n"
            "  → If an error banner is shown: check your email/password."
        )


# ── Browser launcher ──────────────────────────────────────────────────────────

async def launch_browser(headless: bool = True) -> tuple[Browser, BrowserContext, Page]:
    """
    Launch Playwright Chromium with stealth args.
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

    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
    """)

    page = await context.new_page()
    return browser, context, page
