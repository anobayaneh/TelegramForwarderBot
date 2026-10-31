"""
List all your Telegram channels & groups with their numeric IDs.
================================================================
Run:  python list_channels.py

Uses the same .env (and session) as forwarder.py, so log in once first
(either forwarder.py or gen_session.py). Copy the -100... id of any private
channel and use it with /add or /target in the forwarder.
"""

import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID") or os.getenv("API_ID") or 0)
API_HASH = os.getenv("TELEGRAM_API_HASH") or os.getenv("API_HASH") or ""
SESSION_STRING = os.getenv("SESSION_STRING") or os.getenv("STRING_SESSION")
SESSION_NAME = os.getenv("SESSION_NAME", "userbot")

if not API_ID or not API_HASH:
    raise SystemExit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in your .env first.")


async def main():
    session = StringSession(SESSION_STRING) if SESSION_STRING else SESSION_NAME
    client = TelegramClient(session, API_ID, API_HASH)
    await client.start()

    print(f"\n{'ID':>16}  {'TYPE':8}  {'@USERNAME':26}  TITLE")
    print("-" * 90)
    count = 0
    async for d in client.iter_dialogs():
        e = d.entity
        if not (d.is_channel or d.is_group):
            continue
        if getattr(e, "megagroup", False):
            kind = "group"
        elif d.is_channel:
            kind = "channel"
        else:
            kind = "group"
        uname = ("@" + e.username) if getattr(e, "username", None) else "—"
        print(f"{d.id:>16}  {kind:8}  {uname:26}  {d.name}")
        count += 1

    print("-" * 90)
    print(f"{count} channel(s)/group(s). Use the ID (-100…) or @username in the forwarder.\n")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
