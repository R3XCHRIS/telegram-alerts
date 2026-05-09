# Telegram Alerts

A [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) plugin that pushes channel and stream events to a Telegram chat via a bot.

- **Manual test action** — verify your bot before trusting it with real alerts.
- **Event-driven** — subscribes to Dispatcharr's `channel_start`, `channel_stop`, `channel_reconnect`, and `stream_switch` events. Per-event toggles let you control noise.
- **HTML formatting** with safe escaping; a plain-text fallback is also available.
- **Optional enrichment** — opt-in toggles to include the channel's M3U source and the EPG "now playing" title.
- **Zero external dependencies** — uses only the Python standard library.
- **Per-instance label** — tag every message with which Dispatcharr instance it came from.

> Heads up: Telegram Alerts v0.1 is event-driven only. Scheduled health checks (M3U refresh failures, EPG fetch failures, disk usage) are planned for v0.2.

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

Each alert is a short structured message. Example (HTML mode, both enrichment toggles on):

```
🔄 [Yoda] Channel reconnected
Channel: ESPN
Stream: backup-feed
Source: MyIPTVProvider
Now playing: NFL Live
```

Field meanings:

| Field | Source |
|---|---|
| Emoji + label | Per-event severity marker (▶ start, ⏹ stop, 🔄 reconnect, 🔀 switch, ✅ test) |
| `[Yoda]` | The **Instance Label** setting |
| Channel | `payload.channel_name` from Dispatcharr's event |
| Stream  | `payload.stream_name` (only present for `stream_switch`) |
| Source  | M3U account name of the channel's first configured stream — only when **Include Stream Source** is on |
| Now playing | EPG title where `start_time ≤ now < end_time` — only when **Include Current EPG Program** is on |

Optional fields are skipped silently when the lookup returns nothing (e.g. no EPG mapping), so messages stay tidy.

Channel UUIDs are not included — Dispatcharr's event payload doesn't carry them.

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
| Include Stream Source | boolean | off | Adds the M3U account name to each alert. One DB lookup per event. |
| Include Current EPG Program | boolean | off | Adds the currently-airing program title. Requires the channel to have an EPG mapping. One DB lookup per event. |
| Message Format | select | `HTML` | `HTML` or `plain`. |

---

## Actions reference

| Action | Description |
|---|---|
| **Send Test** | Posts a one-off "Telegram Alerts test" message. Validates token, chat ID, and formatting end-to-end. |
| Handle channel/stream event (internal) | Dispatcharr fires this automatically — you should never click it. Hidden in normal use. |

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

## Changelog

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
