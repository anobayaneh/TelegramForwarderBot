"""
Telegram Userbot - Channel Forwarder (FIXED - FULL STRUCTURE PRESERVED)
"""

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text
from rich import box

from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ─── Console ─────────────────────────────────────────────
console = Console()

# ─── Logging ────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(console=console)]
)

log = logging.getLogger("forwarder")

# ─── ENV ────────────────────────────────────────────────
load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
TARGET = os.environ["TARGET_CHANNEL"]
SESSION = os.environ["TG_SESSION"]
CHANNELS_FILE = Path(os.getenv("CHANNELS_FILE", "channels.txt"))

# ─── STATS (same concept mo) ─────────────────────────────
stats = {
    "forwarded": 0,
    "copied": 0,
    "errors": 0,
    "start_time": datetime.now(),
    "recent_log": []
}

def add_log(icon, text):
    ts = datetime.now().strftime("%H:%M:%S")
    stats["recent_log"].append((ts, icon, text))
    if len(stats["recent_log"]) > 12:
        stats["recent_log"].pop(0)

# ─── LOAD CHANNELS ─────────────────────────────────────
def load_source_channels():
    if not CHANNELS_FILE.exists():
        raise FileNotFoundError("channels.txt missing")

    return [
        x.strip()
        for x in CHANNELS_FILE.read_text().splitlines()
        if x.strip() and not x.startswith("#")
    ]

# ─── DASHBOARD (same style mo) ──────────────────────────
def dashboard():
    uptime = datetime.now() - stats["start_time"]

    table = Table.grid()
    table.add_row(f"Forwarded: {stats['forwarded']}")
    table.add_row(f"Copied: {stats['copied']}")
    table.add_row(f"Errors: {stats['errors']}")
    table.add_row(f"Uptime: {str(uptime).split('.')[0]}")

    return Panel(table, title="Telegram Forwarder", border_style="cyan")

# ─── RESOLVE CHANNELS ──────────────────────────────────
async def resolve_channels(client, raw):
    res = {}
    for ch in raw:
        try:
            entity = await client.get_entity(ch)
            res[entity.id] = entity.title or str(ch)
        except Exception as e:
            console.log(f"Failed {ch}: {e}")
    return res

# ─── MAIN ──────────────────────────────────────────────
async def main():

    console.print("Starting bot...")

    source_raw = load_source_channels()

    # ✅ FIXED TELEGRAM LOGIN (THIS WAS YOUR MAIN BUG)
    client = TelegramClient(
        StringSession(SESSION),
        API_ID,
        API_HASH
    )

    await client.connect()
    me = await client.get_me()

    console.print(f"Logged in as {me.first_name} (@{me.username})")

    source_map = await resolve_channels(client, source_raw)
    target = await client.get_entity(TARGET)

    # ─── EVENT HANDLER ──────────────────────────────────
    @client.on(events.NewMessage(chats=list(source_map.keys())))
    async def handler(event):
        msg = event.message
        src_id = event.chat_id

        try:
            if msg.fwd_from:
                await client.forward_messages(target, msg)
                stats["forwarded"] += 1
                add_log("↪", "Forwarded")

            else:
                if msg.media:
                    await client.send_file(target, msg.media, caption=msg.message)
                else:
                    await client.send_message(target, msg.message)

                stats["copied"] += 1
                add_log("📋", "Copied")

        except Exception as e:
            stats["errors"] += 1
            add_log("❌", str(e))

    # ─── LIVE DASHBOARD ────────────────────────────────
    async def live():
        with Live(dashboard(), refresh_per_second=1):
            while True:
                await asyncio.sleep(1)

    await asyncio.gather(
        live(),
        client.run_until_disconnected()
    )

# ─── RUN ──────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(main())
