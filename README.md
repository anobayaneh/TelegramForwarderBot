# Telegram Channel Forwarder

A Telethon **userbot** (your own account) that forwards, copies, backfills and
relays messages into your channel(s) — all controlled live with chat commands.

## Modes

- **Live forward** — new posts from source channels → target(s).
- **Backfill** — dump a channel's history, optionally from a per-channel date.
- **Manual relay** — forward/send anything to an *inbox* chat → it goes out.

## Filters & options

- Type filter: `all | video | image | zip | document`
- Keyword/ad filter (`BAN_WORDS`) and link filter (`DROP_LINKS`)
- Fixed caption: `replace` / `append` / `off`
- Content-protected channels → posts a `This is private` marker (or skips)
- Auto-reconnect + flood-wait pacing (safe for large backfills)
- Edit & delete sync

## Control commands

Send these in your **Saved Messages** (or `CONTROL_CHAT`). Changes apply
instantly — no redeploy — and are saved to `data/settings.json`.

```
/help  /status  /list
/add <ref> [YYYY-MM-DD]      /remove <ref>
/setdate <ref> <date>        /cleardate <ref>
/target me|@chan|-100id[,..]
/mode all|video|image|zip|document
/banwords a,b | /banwords clear
/droplinks on|off
/caption <text> | /caption clear
/captionmode replace|append|off
/privatenotice <text> | /private notice|skip
/setinbox <ref> | /relay on|off
/backfill <ref>|all | /stop
/live on|off
```
`<ref>` = `me` (Saved Messages), `@username`, or a numeric `-100…` id.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env      # edit with your values
python forwarder.py
```
First run asks for your phone + login code (creates `userbot.session`).
Then open your Saved Messages and send `/help`.

## Deploy 24/7 on Railway

> Cloudflare Workers can't run this — a userbot needs an always-on process.
> Use Railway, Fly.io, Render, or a VPS.

1. **Session string** (once, locally): `python gen_session.py` → copy the output.
2. Push this repo to GitHub.
3. Railway → **New Project → Deploy from GitHub repo** (Nixpacks auto-detects).
4. **Variables**: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `SESSION_STRING`,
   `TARGET_CHANNELS`, and any starting options you like.
5. Deploy. It runs headless; each action is logged as a line.

### Keep command changes across redeploys (Railway Volume)

`data/settings.json` and `data/msgmap.db` live under `data/`. On Railway the
disk is **ephemeral**, so changes you make via commands are lost on a redeploy
unless you attach a **Volume**:

1. Railway service → **Settings → Volumes → New Volume**.
2. Mount path: **`/app/data`** (the app runs in `/app`; its `data/` is there).
3. Redeploy. Now `/add`, `/target`, `/caption`, … persist across restarts.

Without a Volume, live commands still work while running — they just reset to
your env defaults after a redeploy.

## Security

- Never commit `.env`, `*.session`, or your `SESSION_STRING`.
- The session string = full access to your account; keep it in host env only.
- Rotate the API hash at https://my.telegram.org if it ever leaked.
- To post into a channel, your account must be an **admin** there; otherwise
  use `me` (Saved Messages) as the target.
- Content protection on a source can't be bypassed — protected media is not
  copied; you get a `This is private` marker instead.
