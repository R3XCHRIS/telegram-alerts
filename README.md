<img src="logo.png" align="right" width="128" alt="Dispatcharr Telegram Alerts logo" />

# Dispatcharr Telegram Alerts

> [!WARNING]
> ## ⚠ Use the `:dev` Dispatcharr image until the next tagged release
>
> This plugin depends on two upstream Dispatcharr fixes that are merged into the `dev` branch but predate the current `:latest` image:
>
> 1. **Event-driven alerts need [Dispatcharr/Dispatcharr#1232](https://github.com/Dispatcharr/Dispatcharr/pull/1232).** Without it, real channel/stream/VOD events silently produce nothing — only the manual **Send Test** action works.
> 2. **The daily report needs [Dispatcharr/Dispatcharr#1245](https://github.com/Dispatcharr/Dispatcharr/pull/1245)**, which registers plugin `@shared_task` decorators with Celery workers. v0.4.4 ships a `queue="dvr"` workaround that keeps the daily report working on `:latest`, but `:dev` removes the need for that routing entirely.
>
> Use the `:dev` image until the next tagged Dispatcharr release ships both fixes:
>
> ```yaml
> # docker-compose.yml
> services:
>   dispatcharr:
>     image: ghcr.io/dispatcharr/dispatcharr:dev
>     # ...rest of your config
> ```
>
> Once a stable release ships #1232 and #1245, this warning will be removed and `min_dispatcharr_version` in `plugin.json` will be bumped accordingly.
>
> **Diagnostic on `:latest`:** if you install and only **Send Test** works while real channel/stream/VOD events produce nothing, you've hit #1232 — it is **not** a bug in Telegram Alerts.

---

A [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) plugin that pushes channel, stream, and VOD events to a Telegram chat via a bot — plus an optional cron-driven daily report covering public IP, geo, bandwidth, activity stats, and source health.

- **Manual test action** — verify your bot before trusting it with real alerts.
- **Event-driven** — subscribes to Dispatcharr's `channel_start`, `channel_stop`, `channel_reconnect`, `stream_switch`, `vod_start`, and `vod_stop` events. Per-event toggles let you control noise.
- **Daily report** (optional) — public IP + geo, Cloudflare speedtest, activity stats since last report, and M3U/EPG source health. Cron-scheduled via Celery beat.
- **HTML formatting** with safe escaping; a plain-text fallback is also available.
- **Optional enrichment** — opt-in toggles to include the channel/VOD's M3U source and the EPG "now playing" title.
- **Zero external dependencies** — uses only the Python standard library.
- **Per-instance label** — tag every message with which Dispatcharr instance it came from.

---

## Install

### Via the Dispatcharr Plugins catalogue (recommended)

Once merged into the [official catalogue](https://github.com/Dispatcharr/Plugins), install from the in-app Plugins tab.

### Manual install

1. Copy this folder to `/data/plugins/telegram-alerts/` inside your Dispatcharr container.
2. Make sure the files are owned by the container's runtime user (typically `dispatch:dispatch`).
3. Restart the container so all worker processes pick up the new code.
4. Enable **Telegram Alerts** in the Plugins tab.

---

## Configure your Telegram bot

You'll need a bot token and a chat ID.

### 1. Create the bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts (give it a name and a unique `_bot` username).
3. BotFather will reply with a token of the form `123456789:ABCdefGhIJklmNOpqrsTUVwxyz`.
4. Copy this token — you'll paste it into the Plugin's **Bot Token** setting.

### 2. Find your chat ID

You can send the alerts to either a personal DM with the bot or a group/channel.

**For a personal DM:**

1. Open Telegram, search for your new bot, and send it any message (e.g. `hello`).
2. In your browser, visit:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
3. Look for `"chat":{"id": 123456789, ...}` in the JSON. That number is your chat ID.

**For a group:**

1. Add the bot to your group.
2. Send any message in the group (the bot needs at least one update to appear).
3. Hit `/getUpdates` as above.
4. The chat ID will be a **negative** number (e.g. `-1001234567890` for supergroups).

> If `/getUpdates` returns an empty array, send another message and try again — Telegram only returns recent updates.

### 3. Plug it in

1. Open Dispatcharr → Plugins → Telegram Alerts → Settings.
2. Paste the **Bot Token** and **Chat ID**.
3. Set the **Instance Label** (optional but useful — e.g. `Yoda` or `Backup-NAS`).
4. Toggle which events you care about under **[ALERTS] Toggles**. Defaults: only `channel_reconnect` is on.
5. Click **Save**.
6. Switch to the **Actions** tab and click **Send Test**. You should see a message arrive in your chat within a second.

---

## What gets sent

Each alert is a short structured message. Example for a channel event (HTML mode, both enrichment toggles on):

```
🔄 [Yoda] Channel reconnected
Channel: ESPN
Stream: backup-feed
Source: MyIPTVProvider
Now playing: NFL Live
```

Example for a VOD event (HTML mode, **Include Stream Source** on):

```
🎬 [Yoda] VOD started
Title: Inception
Source: MyIPTVProvider
```

Field meanings:

| Field | Source |
|---|---|
| Emoji + label | Per-event severity marker (▶ start, ⏹ stop, 🔄 reconnect, 🔀 switch, 🎬 VOD start, 🛑 VOD stop, ✅ test) |
| `[Yoda]` | The **Instance Label** setting |
| Channel | `payload.channel_name` (channel events only) |
| Title | `payload.content_name` (VOD events only — movie or episode title) |
| Stream  | `payload.stream_name` (only present for `stream_switch`) |
| Source  | M3U account name backing the channel or VOD — only when **Include Stream Source** is on |
| Now playing | EPG title where `start_time ≤ now < end_time` — only when **Include Current EPG Program** is on, channels only (VODs have no EPG) |

Optional fields are skipped silently when the lookup returns nothing (e.g. no EPG mapping), so messages stay tidy.

Channel UUIDs are not included — Dispatcharr's event payload doesn't carry them. VOD events do carry a `content_uuid`, used internally to look up the VOD's M3U source.

---

## Settings reference

| Setting | Type | Default | Notes |
|---|---|---|---|
| Bot Token | string | — | From @BotFather. Masked in logs. |
| Chat ID | string | — | Numeric. Group IDs are negative. |
| Instance Label | string | `Dispatcharr` | Prefixed in every message. |
| Alert on Channel Start | boolean | off | Noisy if you have many channels. |
| Alert on Channel Stop | boolean | off | Noisy. |
| Alert on Channel Reconnect | boolean | **on** | Useful warning signal. |
| Alert on Stream Switch | boolean | off | Fires whenever a stream URL is swapped. |
| Alert on VOD Start | boolean | off | Fires every time a movie/episode starts playing. Chatty in multi-user setups. |
| Alert on VOD Stop | boolean | off | Fires every time VOD playback ends. |
| Include Stream Source | boolean | off | Adds the M3U account name to each alert. Channels look up via priority order; VODs look up via the Movie/Episode/Series M3U relation. One DB lookup per event. |
| Include Current EPG Program | boolean | off | Adds the currently-airing program title. Requires the channel to have an EPG mapping. One DB lookup per event. |
| Message Format | select | `HTML` | `HTML` or `plain`. |
| **Enable Daily Report** | boolean | off | Master toggle for the cron-driven daily report. After turning on, click **Apply Schedule** in the Actions tab to register the cron. |
| Report Schedule (cron) | string | `0 9 * * *` | 5-field cron. Default = every day 09:00 in the timezone below. |
| Report Timezone | string | (empty = UTC) | IANA name (e.g. `Europe/London`, `Australia/Brisbane`). Re-Apply after changing. |
| Report Chat ID | string | (empty) | Send report to a different chat than per-event alerts. Blank = use main Chat ID. |
| Include Network Section | boolean | on | Public IP + geographic location lookup. |
| Include Speedtest | boolean | on | Down/up bandwidth via Cloudflare. ~150 MB per test, respects the cooldown below. |
| Speedtest Cooldown (hours) | number | `6` | Minimum hours between speedtests. Lets you run reports hourly without burning bandwidth. |
| Include Activity Section | boolean | on | Channel plays, top channels, VOD plays, errors, stream switches since the previous report. |
| Include Sources Section | boolean | on | M3U account count + EPG source freshness. |

---

## Actions reference

| Action | Description |
|---|---|
| **Send Test** | Posts a one-off "Telegram Alerts test" message. Validates token, chat ID, and formatting end-to-end. |
| **Send Report Now** | Build and send a daily report immediately. Window = since the previous report. Advances the "last report" marker on successful send. |
| **Apply Schedule** | Register or update the cron task in `django-celery-beat`. Re-click after changing any report setting (snapshots fresh settings into the task). |
| **Show Schedule Status** | Show the registered cron, last run, total runs. |
| **Remove Schedule** | Unregister the periodic task. |
| Handle channel/stream/VOD event (internal) | Dispatcharr fires this automatically — you should never click it. Hidden in normal use. |

---

## Troubleshooting

**Test message fails with `Telegram HTTP 401`**
Token is wrong. Re-copy it from @BotFather.

**Test message fails with `Telegram HTTP 400: chat not found`**
The bot has never received a message from that chat. DM the bot once (or post in the group) so Telegram knows the chat exists, then retry.

**Test message succeeds but event alerts never arrive**
1. Check the relevant per-event toggle is on.
2. Confirm Dispatcharr is actually emitting the event (try starting a channel).
3. Look in the Dispatcharr logs for lines starting with `on_event[<event>]`.

**`Unknown action: <id>` after a code change**
Dispatcharr workers cache the plugin module. **Restart the Dispatcharr container** — touching `.reload_token` is not enough.

**Chat ID validation rejects my ID**
The plugin requires a numeric integer, optionally with a leading `-` for groups. Strip any whitespace or quotes.

---

## Development

```bash
git clone https://github.com/R3XCHRIS/telegram-alerts
cd telegram-alerts
pip install pytest
python3 -m pytest tests/
```

The tests cover the pure helpers (token masking, HTML escaping, message formatting, credential validation). Network-dependent code in `_send_telegram` is exercised by the `Send Test` action against the live Telegram API.

---

## Daily report

Off by default. To enable:

1. Tick **Enable Daily Report** in Settings, pick your cron in **Report Schedule** (default `0 9 * * *` = every day at 09:00).
2. Optionally toggle off whichever sections you don't want.
3. Save.
4. Switch to the Actions tab, click **Apply Schedule** — that registers (or updates) a `django-celery-beat` periodic task.
5. Click **Send Report Now** once to verify the format end-to-end.

### Window semantics

The activity stats window is **"since the previous report"**, not a fixed 24 hours. So:

- **Daily cron** → each report covers the last day.
- **Weekly cron** → each report covers the last week.
- **Hourly cron** → each report covers the last hour. Most hours show zero activity — that's signal too.
- **Manual run** → covers everything since the last (manual or scheduled) report.

The first ever report has no baseline and defaults to "last 24h", labelled `(first report)` in the message.

If a Telegram send fails, the "last report" timestamp is **not** advanced — so the next attempt picks up the missed window.

### Speedtest details

- Down: ~100 MB GET from Cloudflare's `speed.cloudflare.com/__down`.
- Up: ~25 MB POST to `speed.cloudflare.com/__up`.
- Together ~150 MB per test. Run frequency capped by **Speedtest Cooldown (hours)** (default 6) regardless of cron tightness — so an hourly cron still only tests bandwidth 4× per day.
- Single-stream measurement — less precise than multi-stream tools like Ookla but adequate for daily trend detection. No external binaries required.

### Persisted state

The plugin writes a small `.state.json` file alongside its code (e.g. `/data/plugins/telegram-alerts/.state.json`) to track `last_report_at` and `last_speedtest_at`. Losing this file (manual delete, container rebuild) is non-fatal — the next report falls back to the 24h default window.

---

## Changelog

### 0.4.4
- Fix: **Daily report never actually sent.** Same upstream Dispatcharr bug as [Dispatcharr#1244](https://github.com/Dispatcharr/Dispatcharr/issues/1244): plugin `@shared_task` registrations don't propagate to Celery's default-queue prefork pool children, so the daily report task — dispatched correctly by beat at the configured cron time — was rejected by every worker with `KeyError: 'telegram_alerts.send_daily_report'`. The `total_run_count` on the periodic task still ticked up, so Show Status looked healthy. As a workaround the plugin now routes its scheduled task to the `dvr` queue (single-process thread pool — does register the task correctly). [Dispatcharr#1245](https://github.com/Dispatcharr/Dispatcharr/pull/1245) is a partial upstream fix; a follow-up using `worker_process_init` instead of `worker_ready` is needed to remove this workaround.
- Fix: **Show Status `last_run_at` was meaningless.** django-celery-beat only updates that field on dispatch, not on completion — so the timestamp moved even when the worker rejected the task. The task now bumps `last_run_at` itself on successful completion. A stale timestamp now correctly indicates a failing schedule.
- **Upgrade note:** if you set up your schedule on 0.4.3 or earlier, click **Apply Schedule** once after upgrading — that rewrites the stored task to add `queue='dvr'`. Without it, beat will keep dispatching to the default queue (where the worker rejects the task).

### 0.4.3
- Geo lookup switched from `ipapi.co` to `ipinfo.io` — ipapi was returning HTTP 403 after a handful of probes (likely free-tier per-IP rate limit). ipinfo gives 50k/month per IP with no auth.
- Network section now includes a **country flag emoji** derived from the 2-letter country code (e.g. 🇬🇧 London, England). The full country name is no longer rendered — the flag carries the same information and is more visually scannable.
- Message format tightened: em-dash separates IP from details, middot separates location from ISP.

### 0.4.2
- Fix: **Speedtest download was still failing** in v0.4.1. Cloudflare's `/__down` rejects single requests over ~75 MB with HTTP 403 (anti-abuse), separate from the User-Agent filter v0.4.1 fixed. Now chunks the download into 4×25 MB sequential GETs — same pattern browser-based speed tests use. Aggregate Mbps = total bytes / total elapsed.

### 0.4.1
- Fix: **Activity section was always empty**. `_collect_activity_stats` queried `SystemEvent.created_at`, but the actual field is `SystemEvent.timestamp`. The bare `except` swallowed the `FieldError` and the section silently rendered "(no data available)".
- Fix: **Speedtest download was always (failed)**. Cloudflare's `/__down` endpoint returns HTTP 403 to non-browser User-Agents. Now uses a Mozilla UA for the speedtest endpoints (and the IP/geo lookups for consistency).
- New setting **Report Timezone**: IANA timezone name (e.g. `Europe/London`). Cron is now interpreted in this timezone instead of always UTC. Blank = UTC (unchanged default).

### 0.4.0
- New optional **daily report** feature: cron-driven digest covering public IP + geographic location, Cloudflare speedtest (down/up), activity stats (channel plays, top 3 channels, VOD plays, errors, stream switches), and source health (M3U accounts, EPG freshness).
- Activity window = "since previous report" — same cron pattern works at any interval from hourly to weekly.
- Speedtest cooldown setting decouples bandwidth use from cron frequency.
- All sections are individually toggleable. None of the existing per-event alert toggles affect the report's stats counting.
- New actions: **Send Report Now**, **Apply Schedule**, **Show Schedule Status**, **Remove Schedule**. The scheduling uses `django-celery-beat` the same way VOD2MLIB does.

### 0.3.0
- New events: subscribes to `vod_start` and `vod_stop`, with their own opt-in toggles. Both off by default.
- VOD alerts use **Title:** for the headline (from `content_name`) instead of **Channel:**.
- **Include Stream Source** now also enriches VOD alerts — looks up the M3U account via Movie/Episode/Series relations.
- The Now-playing EPG enrichment is intentionally suppressed for VOD events (VODs don't have EPG data).

### 0.2.1
- Fix: **Include Stream Source** now returns the channel's *highest-priority* configured stream (the one shown first in Dispatcharr's UI). Previously it returned an arbitrary stream — usually the lowest Stream PK, which on multi-source channels was rarely the user's #1.

### 0.2.0
- New optional enrichment toggles: **Include Stream Source** (M3U account name) and **Include Current EPG Program** (now-playing title). Both off by default; each adds one DB lookup per event when on.
- Lookups degrade silently — a missing EPG mapping or DB hiccup omits the line rather than breaking the alert.

### 0.1.0
- Initial release.
- Manual `Send Test` action.
- Event-driven alerts for `channel_start`, `channel_stop`, `channel_reconnect`, `stream_switch` with per-event toggles.
- HTML and plain-text message formats.
- Per-instance label prefix.

---

## License

MIT — see [LICENSE](LICENSE).
