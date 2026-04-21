"""
storage.py — Apify Dataset, KV Store, and named JSON file helpers.
"""
from __future__ import annotations

import json
from apify import Actor


async def save_question(question: dict, filename: str = "questions") -> None:
    """Push a question to the default Dataset."""
    await Actor.push_data(question)


async def flush_json(questions: list[dict], filename: str) -> None:
    """Save the full questions list as <filename>.json in the KV Store."""
    store = await Actor.open_key_value_store()
    await store.set_value(
        f"{filename}.json",
        json.dumps(questions, indent=2, ensure_ascii=False).encode("utf-8"),
        content_type="application/json; charset=utf-8",
    )
    print(f"[*] Saved {len(questions)} questions → KV:{filename}.json")


async def save_audio(filename: str, data: bytes, mime_type: str) -> str:
    store = await Actor.open_key_value_store()
    await store.set_value(filename, data, content_type=mime_type)
    return f"kv://{filename}"


async def save_state(last_n: int) -> None:
    store = await Actor.open_key_value_store()
    await store.set_value(
        "STATE",
        json.dumps({"last_n": last_n}).encode(),
        content_type="application/json",
    )


async def load_state() -> int:
    try:
        store = await Actor.open_key_value_store()
        raw = await store.get_value("STATE")
        if raw:
            data = json.loads(raw if isinstance(raw, str) else raw.decode())
            return int(data.get("last_n", 0))
    except Exception:
        pass
    return 0
