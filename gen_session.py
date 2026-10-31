"""
Generate a Telethon StringSession for cloud hosting.
=====================================================
Run this ONCE on your own computer:

    python gen_session.py

It will ask for your phone number and the login code Telegram sends you
(and your 2FA password, if you have one). It then prints a long session
string.

Copy that string and set it as the SESSION_STRING environment variable on
your host (e.g. Railway → Variables). Do NOT commit it or paste it anywhere
public — it grants full access to your Telegram account.

API_ID / API_HASH are read from your .env (or environment); you can also just
hard-code them below for this one-off script.
"""

import os

from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID") or os.getenv("API_ID") or 0)
API_HASH = os.getenv("TELEGRAM_API_HASH") or os.getenv("API_HASH") or ""

if not API_ID or not API_HASH:
    raise SystemExit(
        "Set TELEGRAM_API_ID and TELEGRAM_API_HASH in your .env first "
        "(get them from https://my.telegram.org)."
    )

print("\nGenerating Telegram String Session…")
print("Log in with your phone number + the code Telegram sends you.\n")

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    session_string = client.session.save()
    me = client.get_me()
    print("\n✅ SUCCESS! Logged in as:", me.first_name, f"(@{me.username})")
    print("\nYour SESSION_STRING (keep it secret!):\n")
    print(session_string)
    print("\nSet it on your host as the SESSION_STRING variable.\n")
