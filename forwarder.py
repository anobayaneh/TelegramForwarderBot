"""
Telegram Userbot - Channel Forwarder
Forwards messages from resource channels to your public channel.

Rules:
- Forwarded messages → re-forwarded (retains "Forwarded from @x" label)
- Manual (original) messages → copied as plain send (no forwarded label)
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text
from rich import box
from telethon import TelegramClient, events

# ─── Rich Console ──────────────────────────────────────────────────────────────
console = Console()

# ─── Logging (with Rich) ───────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="%H:%M:%S",
    handlers=[RichHandler(console=console, rich_tracebacks=True, markup=True)],
)
log = logging.getLogger("forwarder")

# ─── Config ────────────────────────────────────────────────────────────────────
load_dotenv()

API_ID        = int(os.environ["TELEGRAM_API_ID"])
API_HASH      = os.environ["TELEGRAM_API_HASH"]
TARGET        = os.environ["TARGET_CHANNEL"]
CHANNELS_FILE = Path(os.getenv("CHANNELS_FILE", "channels.txt"))
SESSION_NAME  = os.getenv("SESSION_NAME", "userbot")

# ─── Stats tracker ─────────────────────────────────────────────────────────────
stats = {
    "forwarded": 0,
    "copied": 0,
    "errors": 0,
    "start_time": datetime.now(),
    "last_activity": "—",
    "recent_log": [],          # list of (time_str, emoji, text)
}

MAX_LOG_LINES = 12


def add_log(emoji: str, text: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    stats["recent_log"].append((ts, emoji, text))
    if len(stats["recent_log"]) > MAX_LOG_LINES:
        stats["recent_log"].pop(0)
    stats["last_activity"] = f"{ts} — {text}"


# ─── Dashboard builder ─────────────────────────────────────────────────────────
def build_dashboard(source_channels: list[str], target_title: str) -> Panel:
    uptime = datetime.now() - stats["start_time"]
    hours, rem = divmod(int(uptime.total_seconds()), 3600)
    mins, secs = divmod(rem, 60)
    uptime_str = f"{hours:02d}:{mins:02d}:{secs:02d}"

    total = stats["forwarded"] + stats["copied"]

    # ── Header bar ──────────────────────────────────────────────────────────────
    header = Text()
    header.append("  ✈  TELEGRAM FORWARDER  ", style="bold white on blue")
    header.append(f"  RUNNING  ", style="bold black on green")
    header.append(f"  ⏱ {uptime_str}  ", style="bold white on dark_blue")

    # ── Stats row ───────────────────────────────────────────────────────────────
    stats_table = Table.grid(expand=True, padding=(0, 2))
    stats_table.add_column(justify="center")
    stats_table.add_column(justify="center")
    stats_table.add_column(justify="center")
    stats_table.add_column(justify="center")

    def stat_cell(value, label, color):
        t = Text()
        t.append(f"  {value}  \n", style=f"bold {color}")
        t.append(f"  {label}  ", style=f"dim {color}")
        return t

    stats_table.add_row(
        Panel(stat_cell(total,                  "TOTAL SENT",   "cyan"),    border_style="cyan",   box=box.ROUNDED),
        Panel(stat_cell(stats["forwarded"],     "FORWARDED",    "green"),   border_style="green",  box=box.ROUNDED),
        Panel(stat_cell(stats["copied"],        "COPIED",       "yellow"),  border_style="yellow", box=box.ROUNDED),
        Panel(stat_cell(stats["errors"],        "ERRORS",       "red"),     border_style="red",    box=box.ROUNDED),
    )

    # ── Channels ────────────────────────────────────────────────────────────────
    ch_table = Table(
        title="[bold cyan]📡  Source Channels[/]",
        box=box.SIMPLE_HEAD,
        border_style="blue",
        header_style="bold blue",
        expand=True,
    )
    ch_table.add_column("#", style="dim", width=4)
    ch_table.add_column("Channel", style="bold white")
    ch_table.add_column("Status", justify="center", width=12)

    for i, ch in enumerate(source_channels, 1):
        ch_table.add_row(str(i), ch, "[bold green]● ACTIVE[/]")

    # Target
    target_panel = Panel(
        f"[bold white]{target_title}[/]",
        title="[bold yellow]🎯  Target Channel[/]",
        border_style="yellow",
        box=box.ROUNDED,
    )

    # ── Activity log ────────────────────────────────────────────────────────────
    log_table = Table(
        title="[bold magenta]📋  Activity Log[/]",
        box=box.SIMPLE_HEAD,
        border_style="magenta",
        header_style="bold magenta",
        expand=True,
        show_header=True,
    )
    log_table.add_column("Time",    style="dim", width=10)
    log_table.add_column("",       width=3)
    log_table.add_column("Event",  style="white")

    if stats["recent_log"]:
        for ts, emoji, text in reversed(stats["recent_log"]):
            log_table.add_row(ts, emoji, text)
    else:
        log_table.add_row("—", "💤", "[dim]Waiting for messages…[/]")

    # ── Assemble ────────────────────────────────────────────────────────────────
    from rich.columns import Columns

    layout_table = Table.grid(expand=True, padding=(0, 1))
    layout_table.add_column(ratio=1)
    layout_table.add_column(ratio=1)
    layout_table.add_row(ch_table, target_panel)

    root = Table.grid(expand=True, padding=(0, 0))
    root.add_column()
    root.add_row(header)
    root.add_row(stats_table)
    root.add_row(layout_table)
    root.add_row(log_table)

    return Panel(
        root,
        border_style="bright_blue",
        box=box.DOUBLE_EDGE,
        padding=(0, 1),
    )


# ─── Channel loader ────────────────────────────────────────────────────────────
def load_source_channels() -> list[str]:
    if not CHANNELS_FILE.exists():
        console.print(f"[bold red]✘  channels.txt not found at {CHANNELS_FILE.resolve()}[/]")
        raise FileNotFoundError(f"{CHANNELS_FILE} not found")

    channels = []
    for line in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            channels.append(line)

    if not channels:
        raise ValueError("channels.txt is empty — add at least one source channel.")

    return channels


async def resolve_channels(client: TelegramClient, raw: list[str]) -> dict[int, str]:
    """Resolve usernames / invite links / IDs → {numeric_id: title}."""
    resolved = {}
    for ch in raw:
        try:
            entity = await client.get_entity(ch)
            resolved[entity.id] = getattr(entity, "title", ch)
            console.print(f"  [green]✔[/]  Resolved [bold]{ch}[/] → [dim]id={entity.id}[/] [cyan]{resolved[entity.id]}[/]")
        except Exception as exc:
            console.print(f"  [red]✘[/]  Could not resolve [bold]{ch}[/]: [dim]{exc}[/]")
    return resolved


# ─── Main ──────────────────────────────────────────────────────────────────────
async def main() -> None:
    console.print()
    console.rule("[bold blue]✈  Telegram Channel Forwarder[/]")
    console.print()

    # Load channels
    source_raw = load_source_channels()
    console.print(f"[cyan]→  Loaded [bold]{len(source_raw)}[/] channel(s) from {CHANNELS_FILE}[/]")
    console.print()

    # Connect
    with Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[bold cyan]{task.description}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Connecting to Telegram…", total=None)
        client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
        await client.start()
        me = await client.get_me()
        progress.update(task, description=f"Logged in as [bold green]{me.first_name}[/] (@{me.username})")
        await asyncio.sleep(0.8)

    console.print(f"  [green]✔[/]  Logged in as [bold]{me.first_name}[/] (@{me.username})\n")

    # Resolve source channels
    console.print("[cyan]→  Resolving source channels…[/]")
    source_map = await resolve_channels(client, source_raw)
    if not source_map:
        console.print("[bold red]✘  No source channels could be resolved. Exiting.[/]")
        return

    # Resolve target
    target_entity = await client.get_entity(TARGET)
    target_title  = getattr(target_entity, "title", TARGET)
    console.print(f"\n  [yellow]🎯[/]  Target: [bold]{target_title}[/] [dim](id={target_entity.id})[/]\n")

    # ── Event handler ──────────────────────────────────────────────────────────
    @client.on(events.NewMessage(chats=list(source_map.keys())))
    async def on_new_message(event: events.NewMessage.Event) -> None:
        msg    = event.message
        src_id = event.chat_id
        src_name = source_map.get(src_id, str(src_id))

        try:
            if msg.fwd_from:
                await client.forward_messages(
                    entity=target_entity,
                    messages=msg.id,
                    from_peer=src_id,
                )
                stats["forwarded"] += 1
                add_log("↪", f"[green]Forwarded[/] msg #{msg.id} from [bold]{src_name}[/]")

            else:
                # Original (non-forwarded) message → send as-is, no forward label
                if msg.media:
                    await client.send_file(
                        entity=target_entity,
                        file=msg.media,
                        caption=msg.message or None,  # .message is the raw text (same as .text)
                        force_document=False,
                    )
                elif msg.message:
                    await client.send_message(
                        entity=target_entity,
                        message=msg.message,
                    )
                # If empty message (e.g. sticker with no caption and no text), skip silently
                else:
                    return
                stats["copied"] += 1
                add_log("📋", f"[yellow]Copied[/] msg #{msg.id} from [bold]{src_name}[/]")

        except Exception as exc:
            stats["errors"] += 1
            add_log("❌", f"[red]Error[/] on msg #{msg.id} from [bold]{src_name}[/]: {exc}")

    # ── Live dashboard ─────────────────────────────────────────────────────────
    console.print("[bold green]✔  All systems go! Starting live dashboard…[/]\n")
    await asyncio.sleep(0.5)

    source_display = list(source_map.values()) if source_map else source_raw

    async def live_loop():
        with Live(
            build_dashboard(source_display, target_title),
            console=console,
            refresh_per_second=2,
            screen=True,
        ) as live:
            while True:
                live.update(build_dashboard(source_display, target_title))
                await asyncio.sleep(0.5)

    await asyncio.gather(
        live_loop(),
        client.run_until_disconnected(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[bold yellow]⚠  Stopped by user. Goodbye![/]\n")
