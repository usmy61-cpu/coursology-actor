"""
storage.py — Thin wrappers around Apify Dataset and Key-Value Store.
"""
from __future__ import annotations

import json
from apify import Actor


async def save_question(question: dict) -> None:
    """Push a single question dict to the default Apify Dataset."""
    await Actor.push_data(question)


async def save_audio(filename: str, data: bytes, mime_type: str) -> str:
    """
    Save audio bytes to the default Key-Value Store.
    Returns the public URL of the saved entry (available after run ends).
    """
    store = await Actor.open_key_value_store()
    await store.set_value(filename, data, content_type=mime_type)
    # Build a predictable reference path for the JSON output
    return f"kv://{filename}"


async def save_state(last_n: int) -> None:
    """Persist scrape progress so a re-run can resume."""
    store = await Actor.open_key_value_store()
    await store.set_value("STATE", json.dumps({"last_n": last_n}).encode(), content_type="application/json")


async def load_state() -> int:
    """Return the last successfully scraped question number (0 if none)."""
    try:
        store = await Actor.open_key_value_store()
        raw = await store.get_value("STATE")
        if raw:
            data = json.loads(raw if isinstance(raw, str) else raw.decode())
            return int(data.get("last_n", 0))
    except Exception:
        pass
    return 0
