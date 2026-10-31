"""
Telegram Userbot - Channel Forwarder (control-panel edition)
============================================================
A userbot (your own Telegram account, via Telethon) that:

  • Live-forwards new posts from source channels to target channel(s).
  • Backfills a channel's history (from oldest / a date), per-channel date.
  • Relays anything you forward/send to an "inbox" chat → your channel.
  • Filters by type (video / image / zip / document / all), ban-words, links.
  • Applies a fixed caption (replace / append / off) to everything sent.
  • Handles content-protected channels gracefully ("This is private" marker).

You control it live by sending commands in your Saved Messages (see /help).
Changes take effect immediately on whatever host it runs on (PC, Railway, …)
and are saved to data/settings.json so they survive a restart (use a Railway
Volume on /data to keep them across redeploys).
"""

import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

# ─── Console / logging ────────────────────────────────────────────────────────
console = Console()
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="%H:%M:%S",
    handlers=[RichHandler(console=console, rich_tracebacks=True, markup=False)],
)
log = logging.getLogger("forwarder")

load_dotenv()


# ─── Env helpers ──────────────────────────────────────────────────────────────
def _get(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v is not None and v.strip() != "":
            return v.strip()
    return default


def _split(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in re.split(r"[,\n]", value)
            if p.strip() and not p.strip().startswith("#")]


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


API_ID = int(_get("TELEGRAM_API_ID", "API_ID") or 0)
API_HASH = _get("TELEGRAM_API_HASH", "API_HASH") or ""
SESSION_STRING = _get("SESSION_STRING", "STRING_SESSION")
SESSION_NAME = _get("SESSION_NAME", default="userbot")

CONTROL_CHAT = _get("CONTROL_CHAT", default="me")      # where you type commands
CHANNELS_FILE = Path(_get("CHANNELS_FILE", default="channels.txt"))
SETTINGS_FILE = Path(_get("SETTINGS_FILE", default="data/settings.json"))
MAP_DB = Path(_get("MAP_DB", default="data/msgmap.db"))
DASHBOARD = (_get("DASHBOARD", default="off") or "off").lower()  # on|off (headless default)
BACKFILL_DELAY = float(_get("BACKFILL_DELAY", default="2"))      # seconds between sends

URL_RE = re.compile(r"(https?://|t\.me/|www\.)", re.IGNORECASE)
_LINK_ENTITY_TYPES = ("MessageEntityUrl", "MessageEntityTextUrl")
BOT_TAG = "⁣"  # invisible marker so the bot ignores its own replies


# ─── Settings (env defaults, overlaid by data/settings.json) ──────────────────
def _parse_source_line(line: str) -> tuple[str, str | None]:
    """'@chan | 2026-01-02' or '@chan 2026-01-02' or '@chan' → (ref, date|None)."""
    line = line.strip()
    if "|" in line:
        ref, _, rest = line.partition("|")
        ref, rest = ref.strip(), rest.strip()
        return ref, (rest or None)
    m = re.match(r"^(\S+)\s+(\d{4}-\d{2}-\d{2})$", line)
    if m:
        return m.group(1), m.group(2)
    return line, None


def _default_sources() -> dict[str, str | None]:
    env_src = _split(_get("SOURCE_CHANNELS"))
    out: dict[str, str | None] = {}
    if env_src:
        for item in env_src:
            ref, date = _parse_source_line(item)
            out[ref] = date
    elif CHANNELS_FILE.exists():
        for line in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.strip().startswith("#"):
                ref, date = _parse_source_line(line)
                out[ref] = date
    return out


class Settings:
    FIELDS = ("sources", "targets", "mode", "ban_words", "drop_links",
              "caption", "caption_mode", "private_notice", "private_action",
              "inbox", "relay_enabled", "live_enabled", "keep_forward_label")

    def __init__(self):
        # Defaults from env.
        self.sources: dict[str, str | None] = _default_sources()
        self.targets: list[str] = _split(_get("TARGET_CHANNELS")) or _split(_get("TARGET_CHANNEL"))
        self.mode: str = (_get("FORWARD_MODE", default="all") or "all").lower()
        self.ban_words: list[str] = [w.lower() for w in _split(_get("BAN_WORDS"))]
        self.drop_links: bool = _as_bool(_get("DROP_LINKS"), default=False)
        self.caption: str | None = _get("CAPTION")
        self.caption_mode: str = (_get("CAPTION_MODE", default="replace") or "replace").lower()
        self.private_notice: str = _get("PRIVATE_NOTICE", default="🔒 This is private")
        self.private_action: str = (_get("PRIVATE_ACTION", default="notice") or "notice").lower()
        self.inbox: str | None = _get("INBOX")
        self.relay_enabled: bool = _as_bool(_get("RELAY_ENABLED"), default=False)
        self.live_enabled: bool = _as_bool(_get("LIVE_ENABLED"), default=True)
        # False (default) = always copy → NO "Forwarded from" label.
        self.keep_forward_label: bool = _as_bool(_get("KEEP_FORWARD_LABEL"), default=False)
        self.load()

    def load(self) -> None:
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                for f in self.FIELDS:
                    if f in data:
                        setattr(self, f, data[f])
            except Exception as e:  # noqa: BLE001
                log.warning("Could not read settings.json: %s", e)

    def save(self) -> None:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {f: getattr(self, f) for f in self.FIELDS}
        SETTINGS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


settings = Settings()


# ─── Message-id map (for edit/delete sync) ────────────────────────────────────
_db: sqlite3.Connection | None = None


def init_map_db() -> None:
    global _db
    MAP_DB.parent.mkdir(parents=True, exist_ok=True)
    _db = sqlite3.connect(MAP_DB)
    _db.execute(
        """CREATE TABLE IF NOT EXISTS msgmap (
               src_chat INTEGER, src_id INTEGER,
               tgt_chat INTEGER, tgt_id INTEGER,
               PRIMARY KEY (src_chat, src_id, tgt_chat))"""
    )
    _db.commit()


def map_add(src_chat, src_id, tgt_chat, tgt_id):
    if _db:
        _db.execute("INSERT OR REPLACE INTO msgmap VALUES (?,?,?,?)",
                    (src_chat, src_id, tgt_chat, tgt_id))
        _db.commit()


def map_get(src_chat, src_id):
    if not _db:
        return []
    return _db.execute("SELECT tgt_chat, tgt_id FROM msgmap WHERE src_chat=? AND src_id=?",
                       (src_chat, src_id)).fetchall()


def map_del(src_chat, src_id):
    if _db:
        _db.execute("DELETE FROM msgmap WHERE src_chat=? AND src_id=?", (src_chat, src_id))
        _db.commit()


# ─── Stats ────────────────────────────────────────────────────────────────────
stats = {"forwarded": 0, "copied": 0, "skipped": 0, "private": 0, "errors": 0,
         "start_time": datetime.now()}


# ─── Runtime (resolved entities) ──────────────────────────────────────────────
class RT:
    client: TelegramClient | None = None
    me_id: int | None = None
    control_id: int | None = None
    inbox_id: int | None = None
    target_entities: list = []
    target_ids: set = set()
    src_ids: set = set()
    src_id_to_ref: dict = {}
    src_id_to_date: dict = {}
    bot_msg_ids: set = set()
    backfill_task: asyncio.Task | None = None


# ─── Filters / caption ────────────────────────────────────────────────────────
def type_ok(msg) -> bool:
    mode = settings.mode
    if mode == "all":
        return True
    if mode == "video":
        return msg.video is not None
    if mode == "image":
        return msg.photo is not None
    if mode == "document":
        return msg.document is not None
    if mode == "zip":
        name = (getattr(msg.file, "name", None) or "") if msg.file else ""
        return name.lower().endswith(".zip")
    return True


def passes_filters(msg) -> tuple[bool, str]:
    if not type_ok(msg):
        return False, f"type≠{settings.mode}"
    text = msg.message or ""
    low = text.lower()
    for w in settings.ban_words:
        if w in low:
            return False, f"ban-word '{w}'"
    if settings.drop_links:
        if URL_RE.search(text):
            return False, "link"
        for ent in (msg.entities or []):
            if type(ent).__name__ in _LINK_ENTITY_TYPES:
                return False, "link"
    return True, ""


def final_caption(msg) -> str:
    orig = msg.message or ""
    if settings.caption_mode == "off" or not settings.caption:
        return orig
    if settings.caption_mode == "replace":
        return settings.caption
    if settings.caption_mode == "append":
        return (orig + "\n\n" + settings.caption) if orig else settings.caption
    return orig


def is_protected(msg, chat=None) -> bool:
    if getattr(msg, "noforwards", False):
        return True
    if chat is not None and getattr(chat, "noforwards", False):
        return True
    return False


# ─── Flood-safe call ──────────────────────────────────────────────────────────
async def safe_call(factory, *, what: str):
    for attempt in range(1, 6):
        try:
            return await factory()
        except FloodWaitError as e:
            wait = int(getattr(e, "seconds", 5)) + 2
            log.warning("Flood-wait %ss on %s", wait, what)
            await asyncio.sleep(wait)
        except (ConnectionError, asyncio.TimeoutError) as e:
            backoff = min(5 * attempt, 30)
            log.warning("Conn error on %s: %s — retry in %ss", what, e, backoff)
            await asyncio.sleep(backoff)
    return await factory()


# ─── Delivery ─────────────────────────────────────────────────────────────────
async def deliver(msg, src_id, src_name, *, chat=None):
    """Send one message to every target, honoring caption/protection/forward."""
    client = RT.client

    # Content-protected?  Don't leak the real content.
    if is_protected(msg, chat):
        stats["private"] += 1
        if settings.private_action == "skip":
            log.info("🔒 protected — skipped #%s from %s", msg.id, src_name)
            return
        for tgt in RT.target_entities:
            try:
                sent = await safe_call(
                    lambda t=tgt: client.send_message(t, settings.private_notice),
                    what="private-notice")
                if sent:
                    RT.bot_msg_ids.add(sent.id)
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                log.error("private-notice failed: %s", e)
        log.info("🔒 protected — notice sent for #%s from %s", msg.id, src_name)
        return

    is_fwd = bool(msg.fwd_from)
    want_caption = settings.caption_mode in ("replace", "append") and settings.caption
    # Keep the native "Forwarded from" label only when explicitly asked AND no
    # caption override is needed; otherwise copy (clean, no forward label).
    keep_forward = is_fwd and settings.keep_forward_label and not want_caption
    text = final_caption(msg)
    # Keep original formatting only when we are not overriding the caption.
    entities = msg.entities if (msg.message and settings.caption_mode == "off") else None

    for tgt in RT.target_entities:
        try:
            if keep_forward:
                sent = await safe_call(
                    lambda t=tgt: client.forward_messages(t, msg.id, from_peer=src_id),
                    what="forward")
                stats["forwarded"] += 1
                tag = "↪ forwarded"
            elif msg.media:
                try:
                    sent = await safe_call(
                        lambda t=tgt: client.send_message(
                            t, text or "", file=msg.media,
                            formatting_entities=entities, link_preview=False),
                        what="copy-media")
                except Exception:
                    sent = await safe_call(
                        lambda t=tgt: client.forward_messages(
                            t, msg.id, from_peer=src_id),
                        what="copy-media-fallback")
                stats["copied"] += 1
                tag = "📋 copied-media"
            else:
                if not text:
                    continue
                sent = await safe_call(
                    lambda t=tgt: client.send_message(
                        t, text, formatting_entities=entities),
                    what="copy-text")
                stats["copied"] += 1
                tag = "📋 copied-text"

            for sm in (sent if isinstance(sent, list) else [sent]):
                if sm is not None:
                    RT.bot_msg_ids.add(sm.id)
                    map_add(src_id, msg.id, _peer_id(tgt), sm.id)
            log.info("%s #%s from %s", tag, msg.id, src_name)

        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            log.error("deliver error on #%s from %s: %s", msg.id, src_name, e)


def _peer_id(entity) -> int:
    return int(getattr(entity, "id", entity))


# ─── Backfill ─────────────────────────────────────────────────────────────────
async def backfill(ref: str):
    client = RT.client
    try:
        entity = await client.get_entity(ref)
    except Exception as e:  # noqa: BLE001
        return f"✘ Cannot resolve {ref}: {e}"

    date_str = settings.sources.get(ref)
    offset_date = None
    if date_str:
        try:
            offset_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return f"✘ Bad date for {ref}: {date_str} (use YYYY-MM-DD)"

    name = getattr(entity, "title", ref)
    log.info("⏳ Backfill start: %s (from %s)", name, date_str or "oldest")
    count = 0
    async for msg in client.iter_messages(entity, offset_date=offset_date, reverse=True):
        ok, _ = passes_filters(msg)
        if not ok and not is_protected(msg, entity):
            continue
        await deliver(msg, entity.id, name, chat=entity)
        count += 1
        await asyncio.sleep(BACKFILL_DELAY)
    log.info("✅ Backfill done: %s (%s sent)", name, count)
    return f"✅ Backfill done: {name} — {count} message(s)"


# ─── Resolver ─────────────────────────────────────────────────────────────────
async def resolve_ref(ref: str):
    return await RT.client.get_entity("me" if ref == "me" else ref)


async def rebuild_runtime():
    """Re-resolve sources, targets, inbox from settings into RT."""
    client = RT.client

    # Targets.
    ents, ids = [], set()
    for ref in settings.targets:
        try:
            e = await resolve_ref(ref)
            ents.append(e)
            ids.add(_peer_id(e))
        except Exception as ex:  # noqa: BLE001
            log.warning("target %s unresolved: %s", ref, ex)
    RT.target_entities, RT.target_ids = ents, ids

    # Sources.
    RT.src_ids, RT.src_id_to_ref, RT.src_id_to_date = set(), {}, {}
    for ref, date in settings.sources.items():
        try:
            e = await resolve_ref(ref)
            RT.src_ids.add(e.id)
            RT.src_id_to_ref[e.id] = ref
            RT.src_id_to_date[e.id] = date
        except Exception as ex:  # noqa: BLE001
            log.warning("source %s unresolved: %s", ref, ex)

    # Inbox.
    RT.inbox_id = None
    if settings.inbox:
        try:
            RT.inbox_id = _peer_id(await resolve_ref(settings.inbox))
        except Exception as ex:  # noqa: BLE001
            log.warning("inbox %s unresolved: %s", settings.inbox, ex)


# ─── Command panel ────────────────────────────────────────────────────────────
HELP = """<b>🛠 FORWARDER — COMMANDS</b>
<i>I-type lahat dito sa control chat. Pindutin ang command para ma-copy.</i>

<b>ℹ️ Info</b>
<code>/help</code> → ipakita itong listahan
<code>/status</code> → kasalukuyang settings + bilang na-send
<code>/list</code> → mga source channel + start date nila

<b>📡 Source channels</b>
<code>/add @channel</code> → dagdag source (mula oldest)
<code>/add @channel 2026-01-02</code> → dagdag na may start date
<code>/remove @channel</code> → tanggalin ang source
<code>/setdate @channel 2026-02-01</code> → palitan ang start date
<code>/cleardate @channel</code> → alisin ang date (kunin lahat)

<b>🎯 Target — saan ipapadala</b>
<code>/target me</code> → Saved Messages mo
<code>/target @mychannel</code> → isang channel
<code>/target -1001234567</code> → via numeric ID
<code>/target @a,@b</code> → maramihang target

<b>🧲 Filter — anong klase ipapasa</b>
<code>/mode all</code> → lahat
<code>/mode video</code> → videos lang
<code>/mode image</code> → images lang
<code>/mode zip</code> → .zip files lang
<code>/mode document</code> → lahat ng files
<code>/banwords promo,giveaway</code> → i-skip ang may ganitong salita
<code>/banwords clear</code> → alisin ang ban words
<code>/droplinks on</code> → i-skip ang may link (on/off)

<b>📝 Caption</b>
<code>/caption Follow @mychannel</code> → set fixed caption
<code>/caption clear</code> → walang caption
<code>/captionmode replace</code> → palitan ang orihinal
<code>/captionmode append</code> → idugtong sa orihinal
<code>/captionmode off</code> → gamitin ang orihinal
<code>/forwardlabel off</code> → walang "Forwarded from" (copy, parang orihinal)
<code>/forwardlabel on</code> → panatilihin ang "Forwarded from" label

<b>🔒 Private / protected</b>
<code>/privatenotice This is private</code> → text sa protected posts
<code>/private notice</code> → magpadala ng marker
<code>/private skip</code> → laktawan na lang nang tahimik

<b>📥 Manual relay</b>
<code>/setinbox me</code> → saang chat ka magfo-forward (me/@chat/-100id)
<code>/relay on</code> → buksan ang relay (on/off)

<b>📜 Backfill — history dump</b>
<code>/backfill @channel</code> → i-dump ang history (gamit date niya)
<code>/backfill all</code> → lahat ng sources
<code>/stop</code> → itigil ang kasalukuyang backfill

<b>📡 Live mode</b>
<code>/live on</code> → buksan/isara ang live forward (on/off)"""


async def handle_command(text: str) -> str:
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/help":
        return "HTML::" + HELP

    if cmd == "/status":
        up = str(datetime.now() - stats["start_time"]).split(".")[0]
        esc = html.escape
        targets = esc(", ".join(settings.targets) or "—")
        return "HTML::" + (
            "<b>📊 STATUS</b>\n"
            f"live: <code>{settings.live_enabled}</code>   "
            f"relay: <code>{settings.relay_enabled}</code>\n"
            f"mode: <code>{settings.mode}</code>   "
            f"caption_mode: <code>{settings.caption_mode}</code>   "
            f"forward_label: <code>{settings.keep_forward_label}</code>\n"
            f"caption: <code>{esc(str(settings.caption))}</code>\n"
            f"targets: <code>{targets}</code>\n"
            f"inbox: <code>{esc(str(settings.inbox))}</code>\n"
            f"sources: <code>{len(settings.sources)}</code>   "
            f"ban_words: <code>{len(settings.ban_words)}</code>   "
            f"drop_links: <code>{settings.drop_links}</code>\n"
            f"private: <code>{settings.private_action}</code> "
            f"(<code>{esc(settings.private_notice)}</code>)\n"
            f"sent → fwd:<code>{stats['forwarded']}</code> "
            f"copy:<code>{stats['copied']}</code> "
            f"skip:<code>{stats['skipped']}</code> "
            f"priv:<code>{stats['private']}</code> "
            f"err:<code>{stats['errors']}</code>\n"
            f"uptime: <code>{up}</code>"
        )

    if cmd == "/list":
        if not settings.sources:
            return "Wala pang source. Dagdag: /add @channel"
        lines = "\n".join(
            f"<code>{html.escape(ref)}</code> → <i>{date or 'oldest'}</i>"
            for ref, date in settings.sources.items())
        return "HTML::<b>📡 SOURCES</b>\n" + lines

    if cmd == "/add":
        if not arg:
            return "Usage: /add <ref> [YYYY-MM-DD]"
        ref, date = _parse_source_line(arg)
        if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            return "Bad date. Use YYYY-MM-DD."
        settings.sources[ref] = date
        settings.save()
        await rebuild_runtime()
        return f"✅ Added {ref} [{date or 'oldest'}]"

    if cmd == "/remove":
        if settings.sources.pop(arg, "∅") == "∅":
            return f"Not found: {arg}"
        settings.save()
        await rebuild_runtime()
        return f"🗑 Removed {arg}"

    if cmd == "/setdate":
        p = arg.split()
        if len(p) != 2 or not re.match(r"^\d{4}-\d{2}-\d{2}$", p[1]):
            return "Usage: /setdate <ref> YYYY-MM-DD"
        if p[0] not in settings.sources:
            return f"Not a source: {p[0]}"
        settings.sources[p[0]] = p[1]
        settings.save()
        return f"📅 {p[0]} → {p[1]}"

    if cmd == "/cleardate":
        if arg not in settings.sources:
            return f"Not a source: {arg}"
        settings.sources[arg] = None
        settings.save()
        return f"📅 {arg} → oldest"

    if cmd == "/target":
        refs = _split(arg)
        if not refs:
            return "Usage: /target me | @chan | -100id [,more]"
        settings.targets = refs
        settings.save()
        await rebuild_runtime()
        return f"🎯 Targets → {refs}  (resolved {len(RT.target_entities)})"

    if cmd == "/mode":
        if arg not in {"all", "video", "image", "zip", "document"}:
            return "Usage: /mode all|video|image|zip|document"
        settings.mode = arg
        settings.save()
        return f"🧲 mode → {arg}"

    if cmd == "/banwords":
        settings.ban_words = [] if arg.lower() == "clear" else [w.lower() for w in _split(arg)]
        settings.save()
        return f"🚫 ban_words → {settings.ban_words}"

    if cmd == "/droplinks":
        settings.drop_links = arg.lower() in {"on", "true", "1", "yes"}
        settings.save()
        return f"🔗 drop_links → {settings.drop_links}"

    if cmd == "/caption":
        settings.caption = None if arg.lower() == "clear" else arg
        settings.save()
        return f"📝 caption → {settings.caption!r}"

    if cmd == "/captionmode":
        if arg not in {"replace", "append", "off"}:
            return "Usage: /captionmode replace|append|off"
        settings.caption_mode = arg
        settings.save()
        return f"📝 caption_mode → {arg}"

    if cmd == "/privatenotice":
        if not arg:
            return "Usage: /privatenotice <text>"
        settings.private_notice = arg
        settings.save()
        return f"🔒 private_notice → {arg!r}"

    if cmd == "/private":
        if arg not in {"notice", "skip"}:
            return "Usage: /private notice|skip"
        settings.private_action = arg
        settings.save()
        return f"🔒 private_action → {arg}"

    if cmd == "/setinbox":
        if not arg:
            return "Usage: /setinbox me | @chat | -100id"
        settings.inbox = arg
        settings.save()
        await rebuild_runtime()
        return f"📥 inbox → {arg}  ({'ok' if RT.inbox_id else 'unresolved'})"

    if cmd == "/relay":
        settings.relay_enabled = arg.lower() in {"on", "true", "1", "yes"}
        settings.save()
        return f"📥 relay → {settings.relay_enabled}"

    if cmd == "/live":
        settings.live_enabled = arg.lower() in {"on", "true", "1", "yes"}
        settings.save()
        return f"📡 live → {settings.live_enabled}"

    if cmd == "/forwardlabel":
        settings.keep_forward_label = arg.lower() in {"on", "true", "1", "yes"}
        settings.save()
        state = "ON (keeps 'Forwarded from')" if settings.keep_forward_label \
            else "OFF (clean copy, no label)"
        return f"↪ forward_label → {state}"

    if cmd == "/backfill":
        if RT.backfill_task and not RT.backfill_task.done():
            return "⏳ A backfill is already running. /stop first."
        refs = list(settings.sources.keys()) if arg.lower() == "all" else [arg]
        if not arg:
            return "Usage: /backfill <ref>|all"

        async def _run(rs):
            for r in rs:
                res = await backfill(r)
                await notify(res)

        RT.backfill_task = asyncio.create_task(_run(refs))
        return f"⏳ Backfill started for {len(refs)} channel(s)…"

    if cmd == "/stop":
        if RT.backfill_task and not RT.backfill_task.done():
            RT.backfill_task.cancel()
            return "🛑 Backfill stopped."
        return "Nothing running."

    return "❓ Unknown command. /help"


async def notify(text: str, *, as_html: bool = False) -> None:
    """Send a reply into the control chat (tagged so we ignore our own msg)."""
    try:
        sent = await RT.client.send_message(
            RT.control_id, BOT_TAG + text,
            parse_mode="html" if as_html else None, link_preview=False)
        if sent:
            RT.bot_msg_ids.add(sent.id)
    except Exception as e:  # noqa: BLE001
        log.error("notify failed: %s", e)


# ─── Client factory ───────────────────────────────────────────────────────────
def make_client() -> TelegramClient:
    session = StringSession(SESSION_STRING) if SESSION_STRING else SESSION_NAME
    return TelegramClient(session, API_ID, API_HASH,
                          connection_retries=None, retry_delay=5,
                          auto_reconnect=True, request_retries=5,
                          flood_sleep_threshold=120)


# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    if not API_ID or not API_HASH:
        console.print("[bold red]✘  TELEGRAM_API_ID / TELEGRAM_API_HASH not set.[/]")
        sys.exit(1)

    console.rule("[bold blue]✈  Telegram Channel Forwarder[/]")
    init_map_db()

    RT.client = make_client()
    console.print("[cyan]→  Connecting to Telegram…[/]")
    await RT.client.connect()
    if not await RT.client.is_user_authorized():
        console.print("[yellow]→  Login needed. Type your phone (+63…) then the code "
                      "from the Telegram app.[/]")
    await RT.client.start()
    me = await RT.client.get_me()
    RT.me_id = me.id
    console.print(f"  [green]✔[/]  Logged in as [bold]{me.first_name}[/] (@{me.username})\n")

    # Control chat.
    try:
        RT.control_id = _peer_id(await resolve_ref(CONTROL_CHAT))
    except Exception:
        RT.control_id = me.id

    await rebuild_runtime()
    console.print(f"[cyan]→  sources={len(RT.src_ids)}  targets={len(RT.target_entities)}  "
                  f"inbox={'yes' if RT.inbox_id else 'no'}[/]")
    console.print(f"[dim]   mode={settings.mode}  caption_mode={settings.caption_mode}  "
                  f"live={settings.live_enabled}  relay={settings.relay_enabled}[/]")

    # ── One broad handler for everything (so dynamic changes need no re-register).
    @RT.client.on(events.NewMessage())
    async def router(event):
        msg = event.message
        chat_id = event.chat_id

        # Ignore our own bot replies.
        if msg.id in RT.bot_msg_ids:
            return

        text = msg.message or ""

        # 1) Command (in control chat, starts with '/').
        if chat_id == RT.control_id and text.startswith("/"):
            reply = await handle_command(text)
            as_html = reply.startswith("HTML::")
            if as_html:
                reply = reply[len("HTML::"):]
            await notify(reply, as_html=as_html)
            return

        # Never treat a target as a source/inbox (avoid loops).
        if chat_id in RT.target_ids:
            return

        # 2) Manual relay inbox.
        if settings.relay_enabled and RT.inbox_id is not None and chat_id == RT.inbox_id:
            ok, reason = passes_filters(msg)
            if not ok:
                stats["skipped"] += 1
                log.info("🚫 relay skip #%s (%s)", msg.id, reason)
                return
            await deliver(msg, chat_id, "inbox", chat=await event.get_chat())
            return

        # 3) Live forward from sources.
        if settings.live_enabled and chat_id in RT.src_ids:
            ok, reason = passes_filters(msg)
            name = RT.src_id_to_ref.get(chat_id, str(chat_id))
            if not ok:
                stats["skipped"] += 1
                log.info("🚫 skip #%s from %s (%s)", msg.id, name, reason)
                return
            await deliver(msg, chat_id, name, chat=await event.get_chat())

    # ── Edit sync.
    @RT.client.on(events.MessageEdited())
    async def on_edit(event):
        if not settings.live_enabled or event.chat_id not in RT.src_ids:
            return
        mapped = map_get(event.chat_id, event.message.id)
        if not mapped:
            return
        new_text = final_caption(event.message)
        for tgt_chat, tgt_id in mapped:
            try:
                await safe_call(lambda c=tgt_chat, i=tgt_id:
                                RT.client.edit_message(c, i, new_text), what="edit-sync")
            except Exception as e:  # noqa: BLE001
                log.error("edit-sync failed: %s", e)
        log.info("✏️ synced edit of #%s", event.message.id)

    # ── Delete sync.
    @RT.client.on(events.MessageDeleted())
    async def on_delete(event):
        if not settings.live_enabled or not event.chat_id:
            return
        for del_id in event.deleted_ids:
            mapped = map_get(event.chat_id, del_id)
            for tgt_chat, tgt_id in mapped:
                try:
                    await safe_call(lambda c=tgt_chat, i=tgt_id:
                                    RT.client.delete_messages(c, [i]), what="delete-sync")
                except Exception as e:  # noqa: BLE001
                    log.error("delete-sync failed: %s", e)
            if mapped:
                map_del(event.chat_id, del_id)
                log.info("🗑 synced delete of #%s", del_id)

    console.print("[bold green]✔  Running. Send /help in your control chat.[/]\n")
    await notify("✅ Forwarder online. Send /help for commands.")
    await RT.client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[bold yellow]⚠  Stopped by user. Goodbye![/]\n")
