"""
Telegram Alerts — Dispatcharr plugin
(slug: telegram-alerts)
v0.4.0 — adds optional daily report (public IP + geo + speedtest +
         activity + source health), with Celery-beat cron scheduling
         and a "since previous report" stats window.

MIT License
Copyright (c) 2026 R3XCHRIS
https://github.com/R3XCHRIS/telegram-alerts
"""
import datetime as _dt
import html as _html
import json
import logging
import os as _os
import re
import socket
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


# Events Dispatcharr emits that this plugin subscribes to. Channel events
# carry `channel_name` (and `stream_name` for stream_switch) in the payload.
# VOD events carry `content_name` and `content_uuid` instead — VODs aren't
# channels and Dispatcharr's `dispatch_event_system` doesn't enrich them
# with channel-side fields.
EVENT_NAMES = (
    "channel_start", "channel_stop", "channel_reconnect", "stream_switch",
    "vod_start", "vod_stop",
)

VOD_EVENTS = frozenset({"vod_start", "vod_stop"})

# Per-event presentation: emoji, severity label, settings-toggle key.
EVENT_META = {
    "channel_start":     {"emoji": "▶",  "label": "Channel started",     "toggle": "alert_channel_start"},
    "channel_stop":      {"emoji": "⏹",  "label": "Channel stopped",     "toggle": "alert_channel_stop"},
    "channel_reconnect": {"emoji": "🔄", "label": "Channel reconnected", "toggle": "alert_channel_reconnect"},
    "stream_switch":     {"emoji": "🔀", "label": "Stream switched",     "toggle": "alert_stream_switch"},
    "vod_start":         {"emoji": "🎬", "label": "VOD started",         "toggle": "alert_vod_start"},
    "vod_stop":          {"emoji": "🛑", "label": "VOD stopped",         "toggle": "alert_vod_stop"},
}

TELEGRAM_API = "https://api.telegram.org"
HTTP_TIMEOUT_SECS = 10

# ----- Daily report constants ------------------------------------------------

IPIFY_URL = "https://api.ipify.org?format=json"
IPAPI_TEMPLATE = "https://ipapi.co/{ip}/json/"
SPEEDTEST_DOWN_URL = "https://speed.cloudflare.com/__down?bytes={bytes}"
SPEEDTEST_UP_URL = "https://speed.cloudflare.com/__up"
SPEEDTEST_DOWN_BYTES = 100_000_000  # 100 MB
SPEEDTEST_UP_BYTES = 25_000_000     # 25 MB (uploads are slower; keep under 30s on residential)
SPEEDTEST_TIMEOUT_SECS = 120
REPORT_HTTP_TIMEOUT_SECS = 15

# Path to a small state file alongside the plugin code. Used to persist
# last_report_at and last_speedtest_at across container restarts without
# muddying PluginConfig.settings (which the user edits via the UI).
PLUGIN_STATE_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".state.json")

# If we have no record of when the last report was sent (first run, state
# file deleted), default the window to this many hours so the first report
# isn't a useless empty digest.
DEFAULT_FIRST_REPORT_WINDOW_HOURS = 24


class Plugin:
    """Send Dispatcharr alerts to a Telegram chat."""

    name = "Telegram Alerts"
    version = "0.4.0"
    description = (
        "Push Dispatcharr channel/stream/VOD events to a Telegram chat via a bot. "
        "Includes a manual test action, per-event toggles, and an optional "
        "cron-driven daily report (public IP + geo + speedtest + activity + source health)."
    )
    author = "R3XCHRIS"
    help_url = "https://github.com/R3XCHRIS/telegram-alerts#readme"

    # Identifiers for the periodic-report Celery task and its django-celery-beat row.
    SCHEDULE_TASK_NAME = "telegram_alerts.daily_report"
    SCHEDULED_TASK_CELERY_NAME = "telegram_alerts.send_daily_report"

    # ----- Settings (Settings tab) ----------------------------------------

    fields = [
        {
            "id": "_about",
            "label": "About",
            "type": "info",
            "description": (
                "Setup:\n"
                "  1. Create a bot with @BotFather on Telegram, copy the token.\n"
                "  2. Send any message to your bot (or add it to a group).\n"
                "  3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates and copy chat.id.\n"
                "  4. Paste both below, Save, then Actions → Send Test Message.\n\n"
                "Docs: https://github.com/R3XCHRIS/telegram-alerts"
            ),
        },
        {
            "id": "_section_telegram",
            "label": "[TELEGRAM]",
            "type": "info",
            "description": "Bot credentials and instance identity.",
        },
        {
            "id": "bot_token",
            "label": "Bot Token (REQUIRED)",
            "type": "string",
            "default": "",
            "placeholder": "123456:ABC-DEF...",
            "help_text": "Telegram bot token from @BotFather. Masked in logs.",
        },
        {
            "id": "chat_id",
            "label": "Chat ID (REQUIRED)",
            "type": "string",
            "default": "",
            "placeholder": "-1001234567890 or 123456789",
            "help_text": "Numeric chat ID. Find via /getUpdates after messaging the bot. Group IDs are negative.",
        },
        {
            "id": "instance_label",
            "label": "Instance Label",
            "type": "string",
            "default": "Dispatcharr",
            "help_text": "Prefixed in every message, e.g. '[Yoda]'. Useful when multiple Dispatcharr instances send to the same chat.",
        },
        {
            "id": "_section_alerts",
            "label": "[ALERTS] Toggles",
            "type": "info",
            "description": "Pick which channel/stream events generate Telegram messages.",
        },
        {
            "id": "alert_channel_start",
            "label": "Alert on Channel Start",
            "type": "boolean",
            "default": False,
            "help_text": "Off by default — can be noisy with many channels.",
        },
        {
            "id": "alert_channel_stop",
            "label": "Alert on Channel Stop",
            "type": "boolean",
            "default": False,
            "help_text": "Off by default — can be noisy.",
        },
        {
            "id": "alert_channel_reconnect",
            "label": "Alert on Channel Reconnect",
            "type": "boolean",
            "default": True,
            "help_text": "On by default — usually a useful warning signal for flaky upstreams.",
        },
        {
            "id": "alert_stream_switch",
            "label": "Alert on Stream Switch",
            "type": "boolean",
            "default": False,
            "help_text": "Off by default — fires whenever a stream URL is swapped.",
        },
        {
            "id": "alert_vod_start",
            "label": "Alert on VOD Start",
            "type": "boolean",
            "default": False,
            "help_text": "Off by default — fires every time a movie/episode starts playing. Can be chatty in multi-user setups.",
        },
        {
            "id": "alert_vod_stop",
            "label": "Alert on VOD Stop",
            "type": "boolean",
            "default": False,
            "help_text": "Off by default — fires every time VOD playback ends.",
        },
        {
            "id": "_section_enrichment",
            "label": "[ENRICHMENT]",
            "type": "info",
            "description": "Optionally include extra context per alert. Each toggle adds one DB lookup per event.",
        },
        {
            "id": "include_stream_source",
            "label": "Include Stream Source",
            "type": "boolean",
            "default": False,
            "help_text": "Add the M3U account name (the channel's first configured stream's source) to each alert.",
        },
        {
            "id": "include_current_program",
            "label": "Include Current EPG Program",
            "type": "boolean",
            "default": False,
            "help_text": "Add the currently-airing program title from EPG data to each alert. Requires the channel to have an EPG mapping.",
        },
        {
            "id": "_section_format",
            "label": "[FORMAT]",
            "type": "info",
            "description": "Message rendering.",
        },
        {
            "id": "message_format",
            "label": "Message Format",
            "type": "select",
            "default": "HTML",
            "options": [
                {"value": "HTML", "label": "HTML (bold, code formatting)"},
                {"value": "plain", "label": "Plain text"},
            ],
            "help_text": "HTML uses Telegram's HTML parse mode. Plain disables formatting.",
        },
        {
            "id": "_section_reports",
            "label": "[DAILY REPORT]",
            "type": "info",
            "description": "Optional cron-driven digest. Stats window = since previous report, so a weekly cron gets a week of stats.",
        },
        {
            "id": "report_enabled",
            "label": "Enable Daily Report",
            "type": "boolean",
            "default": False,
            "help_text": "Master toggle. When on, click 'Apply Schedule' below to register the cron.",
        },
        {
            "id": "report_cron",
            "label": "Report Schedule (cron)",
            "type": "string",
            "default": "0 9 * * *",
            "help_text": "5-field cron: 'minute hour day-of-month month day-of-week'. Default '0 9 * * *' = every day at 09:00. Used by Apply Schedule.",
        },
        {
            "id": "report_chat_id",
            "label": "Report Chat ID (optional)",
            "type": "string",
            "default": "",
            "placeholder": "leave blank to use main Chat ID",
            "help_text": "Send the daily report to a different chat than per-event alerts. Leave blank to use the main Chat ID.",
        },
        {
            "id": "report_include_network",
            "label": "Include Network Section",
            "type": "boolean",
            "default": True,
            "help_text": "Public IP + geographic location lookup.",
        },
        {
            "id": "report_include_speedtest",
            "label": "Include Speedtest",
            "type": "boolean",
            "default": True,
            "help_text": "Down/up bandwidth measurement via Cloudflare (~150 MB per test). Only runs when the Network section is also on, and respects the cooldown below.",
        },
        {
            "id": "report_speedtest_cooldown_hours",
            "label": "Speedtest Cooldown (hours)",
            "type": "number",
            "default": 6,
            "help_text": "Minimum hours between speedtest runs. Stops hourly crons from burning bandwidth. Other report sections still update every tick.",
        },
        {
            "id": "report_include_activity",
            "label": "Include Activity Section",
            "type": "boolean",
            "default": True,
            "help_text": "Channel plays, top channels, VOD plays, errors, stream switches since the previous report.",
        },
        {
            "id": "report_include_sources",
            "label": "Include Sources Section",
            "type": "boolean",
            "default": True,
            "help_text": "M3U account count and EPG source freshness.",
        },
    ]

    # ----- Actions (Actions tab) ------------------------------------------

    actions = [
        {
            "id": "send_test",
            "label": "[ALERT] Send test message",
            "description": "Send a test message to the configured chat. Verifies token, chat ID, and formatting.",
            "button_label": "Send Test",
            "button_variant": "filled",
            "button_color": "blue",
        },
        {
            "id": "send_report_now",
            "label": "[REPORT] Send report now",
            "description": "Build and send a daily report immediately. Window = since the previous report.",
            "button_label": "Send Report",
            "button_variant": "filled",
            "button_color": "blue",
        },
        {
            "id": "apply_schedule",
            "label": "[REPORT] Apply / Update schedule",
            "description": "Register or update the cron task. Re-click after changing any report setting.",
            "button_label": "Apply",
            "button_variant": "outline",
            "button_color": "blue",
        },
        {
            "id": "schedule_status",
            "label": "[REPORT] Show schedule status",
            "description": "Show registered cron, last run, total runs.",
            "button_label": "Status",
            "button_variant": "outline",
            "button_color": "blue",
        },
        {
            "id": "remove_schedule",
            "label": "[REPORT] Remove schedule",
            "description": "Unregister the periodic report task.",
            "button_label": "Remove",
            "button_variant": "outline",
            "button_color": "orange",
            "confirm": {
                "required": True,
                "title": "Remove daily report schedule?",
                "message": "This unregisters the periodic task. You can re-create it any time with Apply.",
            },
        },
        {
            "id": "on_event",
            "label": "Handle channel/stream/VOD event (internal)",
            "description": "Triggered by Dispatcharr on channel/stream/VOD events. Don't run manually.",
            "events": list(EVENT_NAMES),
        },
    ]

    # ----- Action dispatch ------------------------------------------------

    def run(self, action: str, params: dict, context: dict) -> Dict[str, Any]:
        logger = context.get("logger") or logging.getLogger("telegram_alerts")
        settings = context.get("settings") or {}
        params = params or {}

        if action == "send_test":
            return self._action_send_test(settings, logger)
        if action == "on_event":
            return self._handle_event(params, settings, logger)
        if action == "send_report_now":
            return self._action_send_report(settings, logger)
        if action == "apply_schedule":
            return self._action_apply_schedule(settings, logger)
        if action == "remove_schedule":
            return self._action_remove_schedule(settings, logger)
        if action == "schedule_status":
            return self._action_schedule_status(settings, logger)

        return {"status": "error", "message": f"Unknown action: {action}"}

    # ----- Action: send_test ----------------------------------------------

    def _action_send_test(self, settings: Dict[str, Any], logger) -> Dict[str, Any]:
        token = (settings.get("bot_token") or "").strip()
        chat_id = (settings.get("chat_id") or "").strip()
        label = (settings.get("instance_label") or "Dispatcharr").strip() or "Dispatcharr"
        fmt = (settings.get("message_format") or "HTML").strip() or "HTML"

        ok, err = self._validate_credentials(token, chat_id)
        if not ok:
            logger.error("send_test: %s", err)
            return {"status": "error", "message": err}

        text = self._format_test_message(label, fmt)
        logger.info(
            "send_test: posting to chat=%s token=%s fmt=%s",
            chat_id, self._mask_token(token), fmt,
        )

        ok, message = self._send_telegram(token, chat_id, text, fmt, logger)
        if ok:
            return {"status": "ok", "message": f"Test message sent to chat {chat_id}."}
        return {"status": "error", "message": message}

    # ----- Action: on_event -----------------------------------------------

    def _handle_event(
        self, params: dict, settings: Dict[str, Any], logger
    ) -> Dict[str, Any]:
        """Dispatcharr fires this for each subscribed event.

        Payload shape (from `dispatch_event_system`):
          params["event"]   → event name string
          params["payload"] → dict; always has `channel_name`,
                              may have `stream_name`, `stream_id`, etc.
        Channel UUID is NOT included in the payload.
        """
        event = params.get("event") or ""
        payload = params.get("payload") or {}

        meta = EVENT_META.get(event)
        if not meta:
            # Subscribed to an event we don't have metadata for — be quiet.
            logger.warning("on_event: ignoring unknown event %r", event)
            return {"status": "ok", "message": f"Ignored unknown event {event!r}"}

        toggle_key = meta["toggle"]
        if not bool(settings.get(toggle_key)):
            logger.info("on_event: %s suppressed (toggle %s = false)", event, toggle_key)
            return {"status": "ok", "message": f"{event} suppressed by toggle"}

        token = (settings.get("bot_token") or "").strip()
        chat_id = (settings.get("chat_id") or "").strip()
        label = (settings.get("instance_label") or "Dispatcharr").strip() or "Dispatcharr"
        fmt = (settings.get("message_format") or "HTML").strip() or "HTML"

        ok, err = self._validate_credentials(token, chat_id)
        if not ok:
            logger.error("on_event[%s]: %s", event, err)
            return {"status": "error", "message": err}

        is_vod = event in VOD_EVENTS
        primary_id = payload.get("content_name") if is_vod else payload.get("channel_name")

        if settings.get("include_stream_source"):
            source = (
                self._lookup_vod_source(payload.get("content_uuid"))
                if is_vod
                else self._lookup_stream_source(payload.get("channel_name"))
            )
        else:
            source = None

        # EPG "now playing" only applies to live channels — VODs have no EPG.
        program = (
            self._lookup_current_program(payload.get("channel_name"))
            if settings.get("include_current_program") and not is_vod
            else None
        )

        text = self._format_event_message(
            event, payload, label, fmt, source=source, program=program,
        )
        logger.info(
            "on_event[%s]: target=%s source=%s program=%s sending to chat=%s",
            event, primary_id, source, program, chat_id,
        )

        ok, message = self._send_telegram(token, chat_id, text, fmt, logger)
        if ok:
            return {"status": "ok", "message": f"Sent {event} alert."}
        return {"status": "error", "message": message}

    # ----- Pure helpers (unit-tested) -------------------------------------

    @staticmethod
    def _mask_token(token: str) -> str:
        """Redact the secret half of a Telegram bot token for log output.

        Telegram tokens look like `<bot_id>:<secret>`. The bot_id is a public
        integer (it's literally the bot's user ID); the secret after the colon
        is what authenticates. We keep the bot_id and the last 4 of the secret
        so log lines remain debuggable, and replace the rest with '***'.
        """
        if not token:
            return ""
        if ":" not in token:
            return "***"
        bot_id, _, secret = token.partition(":")
        if len(secret) <= 4:
            return f"{bot_id}:***"
        return f"{bot_id}:***{secret[-4:]}"

    @staticmethod
    def _validate_credentials(token: str, chat_id: str) -> Tuple[bool, Optional[str]]:
        if not token:
            return False, "Bot Token is required. Configure it in Settings and click Save."
        if ":" not in token or len(token) < 20:
            return False, "Bot Token looks malformed (expected '<bot_id>:<secret>')."
        if not chat_id:
            return False, "Chat ID is required. Configure it in Settings and click Save."
        # Chat IDs are integers (groups are negative). Accept optional leading -.
        if not re.fullmatch(r"-?\d+", chat_id):
            return False, "Chat ID must be a numeric integer (e.g. 123456789 or -1001234567890)."
        return True, None

    @staticmethod
    def _escape_html(text: str) -> str:
        """Escape user-supplied text for Telegram HTML parse mode.

        Telegram's HTML mode requires `<`, `>`, `&` escaped in text content.
        `html.escape` covers all three (and quotes — harmless extra).
        """
        if text is None:
            return ""
        return _html.escape(str(text), quote=False)

    @classmethod
    def _format_test_message(cls, instance_label: str, fmt: str) -> str:
        if fmt == "HTML":
            return (
                f"✅ <b>[{cls._escape_html(instance_label)}] Telegram Alerts test</b>\n"
                f"If you can read this, your bot token, chat ID, and HTML formatting all work."
            )
        return (
            f"[OK] [{instance_label}] Telegram Alerts test\n"
            f"If you can read this, your bot token and chat ID work."
        )

    @classmethod
    def _format_event_message(
        cls,
        event: str,
        payload: Dict[str, Any],
        instance_label: str,
        fmt: str,
        source: Optional[str] = None,
        program: Optional[str] = None,
    ) -> str:
        meta = EVENT_META.get(event, {"emoji": "•", "label": event})
        emoji = meta["emoji"]
        label = meta["label"]

        # VOD events use a different payload shape: `content_name` instead
        # of `channel_name`, and never carry `stream_name` / EPG data.
        is_vod = event in VOD_EVENTS
        if is_vod:
            primary_label = "Title"
            primary_value = payload.get("content_name") or "(unknown)"
            stream = None
        else:
            primary_label = "Channel"
            primary_value = payload.get("channel_name") or "(unknown)"
            stream = payload.get("stream_name")

        if fmt == "HTML":
            lines = [
                f"{emoji} <b>[{cls._escape_html(instance_label)}] {cls._escape_html(label)}</b>",
                f"{primary_label}: <code>{cls._escape_html(primary_value)}</code>",
            ]
            if stream:
                lines.append(f"Stream: <code>{cls._escape_html(stream)}</code>")
            if source:
                lines.append(f"Source: <code>{cls._escape_html(source)}</code>")
            if program:
                lines.append(f"Now playing: <code>{cls._escape_html(program)}</code>")
            return "\n".join(lines)

        lines = [
            f"{emoji} [{instance_label}] {label}",
            f"{primary_label}: {primary_value}",
        ]
        if stream:
            lines.append(f"Stream: {stream}")
        if source:
            lines.append(f"Source: {source}")
        if program:
            lines.append(f"Now playing: {program}")
        return "\n".join(lines)

    # ----- Dispatcharr DB lookups (not unit-tested — Django-dependent) ----

    @staticmethod
    def _lookup_stream_source(channel_name: Optional[str]) -> Optional[str]:
        """Return the M3U account name of the channel's highest-priority
        configured stream (the one shown first in Dispatcharr's channel UI).

        Channel-to-stream is M2M through `ChannelStream`, which has an
        `order` field set by the user. Django's M2M reverse access does
        NOT auto-apply the through-model's Meta.ordering, so we order
        explicitly by `channelstream__order`.

        Returns None for any failure so a lookup hiccup never breaks the
        alert.
        """
        if not channel_name:
            return None
        try:
            from apps.channels.models import Channel
            channel = Channel.objects.filter(name=channel_name).first()
            if not channel:
                return None
            stream = channel.streams.all().order_by("channelstream__order").first()
            if not stream or not getattr(stream, "m3u_account", None):
                return None
            return stream.m3u_account.name or None
        except Exception:
            return None

    @staticmethod
    def _lookup_vod_source(content_uuid: Optional[str]) -> Optional[str]:
        """Return the M3U account name backing a VOD (movie / series /
        episode) identified by `content_uuid`. The VOD event payload
        identifies content by UUID without telling us which model it
        belongs to, so we try Movie, Episode, then Series in turn.

        Returns None for any failure so the alert never breaks.
        """
        if not content_uuid:
            return None
        try:
            from apps.vod.models import (
                Movie, Series, Episode,
                M3UMovieRelation, M3USeriesRelation, M3UEpisodeRelation,
            )
            movie = Movie.objects.filter(uuid=content_uuid).first()
            if movie:
                rel = (
                    M3UMovieRelation.objects.filter(movie=movie)
                    .select_related("m3u_account").first()
                )
                return rel.m3u_account.name if rel and rel.m3u_account else None
            episode = Episode.objects.filter(uuid=content_uuid).first()
            if episode:
                rel = (
                    M3UEpisodeRelation.objects.filter(episode=episode)
                    .select_related("m3u_account").first()
                )
                return rel.m3u_account.name if rel and rel.m3u_account else None
            series = Series.objects.filter(uuid=content_uuid).first()
            if series:
                rel = (
                    M3USeriesRelation.objects.filter(series=series)
                    .select_related("m3u_account").first()
                )
                return rel.m3u_account.name if rel and rel.m3u_account else None
            return None
        except Exception:
            return None

    @staticmethod
    def _lookup_current_program(channel_name: Optional[str]) -> Optional[str]:
        """Return the title of the program currently airing on this channel
        per its EPG data, or None if no EPG mapping / no matching program."""
        if not channel_name:
            return None
        try:
            from apps.channels.models import Channel
            from django.utils import timezone
            channel = Channel.objects.filter(name=channel_name).first()
            if not channel or not channel.epg_data_id:
                return None
            now = timezone.now()
            program = channel.epg_data.programs.filter(
                start_time__lte=now, end_time__gt=now
            ).first()
            if not program:
                return None
            return program.title or None
        except Exception:
            return None

    # ----- HTTP (network — not unit-tested) -------------------------------

    @classmethod
    def _send_telegram(
        cls,
        token: str,
        chat_id: str,
        text: str,
        fmt: str,
        logger,
    ) -> Tuple[bool, str]:
        url = f"{TELEGRAM_API}/bot{token}/sendMessage"
        body: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if fmt == "HTML":
            body["parse_mode"] = "HTML"

        encoded = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=encoded,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECS) as resp:
                resp_body = resp.read().decode("utf-8", errors="replace")
                parsed = json.loads(resp_body) if resp_body else {}
                if parsed.get("ok"):
                    return True, "ok"
                desc = parsed.get("description") or "unknown error"
                logger.error("telegram API rejected: %s", desc)
                return False, f"Telegram API error: {desc}"
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            desc = err_body
            try:
                desc = json.loads(err_body).get("description") or err_body
            except Exception:
                pass
            logger.error("telegram HTTP %s: %s", exc.code, desc)
            return False, f"Telegram HTTP {exc.code}: {desc[:120]}"
        except urllib.error.URLError as exc:
            logger.error("telegram URL error: %s", exc.reason)
            return False, f"Network error reaching Telegram: {exc.reason}"
        except socket.timeout:
            logger.error("telegram timeout after %ss", HTTP_TIMEOUT_SECS)
            return False, f"Telegram request timed out after {HTTP_TIMEOUT_SECS}s."
        except Exception as exc:
            logger.exception("telegram unexpected error")
            return False, f"Unexpected error: {exc}"

    # ===== Daily report ===================================================

    # ----- Action: send_report_now ---------------------------------------

    def _action_send_report(self, settings: Dict[str, Any], logger) -> Dict[str, Any]:
        token = (settings.get("bot_token") or "").strip()
        # The report can target a different chat than per-event alerts.
        report_chat_id = (settings.get("report_chat_id") or "").strip() or (settings.get("chat_id") or "").strip()
        label = (settings.get("instance_label") or "Dispatcharr").strip() or "Dispatcharr"
        fmt = (settings.get("message_format") or "HTML").strip() or "HTML"

        ok, err = self._validate_credentials(token, report_chat_id)
        if not ok:
            logger.error("send_report: %s", err)
            return {"status": "error", "message": err}

        now = _dt.datetime.now(_dt.timezone.utc)
        state = self._load_plugin_state()
        window_start = self._parse_iso(state.get("last_report_at")) or (
            now - _dt.timedelta(hours=DEFAULT_FIRST_REPORT_WINDOW_HOURS)
        )
        is_first_report = state.get("last_report_at") is None

        report = self._build_report(
            settings=settings,
            window_start=window_start,
            now=now,
            is_first_report=is_first_report,
            logger=logger,
        )

        text = self._format_report_message(report, label, fmt)
        logger.info(
            "send_report: window=%s..%s chat=%s",
            window_start.isoformat(), now.isoformat(), report_chat_id,
        )

        ok, message = self._send_telegram(token, report_chat_id, text, fmt, logger)
        if not ok:
            return {"status": "error", "message": message}

        # Only advance last_report_at on successful send.
        state["last_report_at"] = now.isoformat()
        if report.get("speedtest_ran"):
            state["last_speedtest_at"] = now.isoformat()
        self._save_plugin_state(state)

        return {"status": "ok", "message": f"Report sent to chat {report_chat_id}."}

    # ----- Action: apply / remove / status schedule ----------------------

    def _action_apply_schedule(self, settings: Dict[str, Any], logger) -> Dict[str, Any]:
        cron_expr = (settings.get("report_cron") or "0 9 * * *").strip()
        try:
            minute, hour, dom, month, dow = self._parse_cron(cron_expr)
        except ValueError as exc:
            logger.error("apply_schedule: invalid cron %r: %s", cron_expr, exc)
            return {"status": "error", "message": str(exc)}

        try:
            from django_celery_beat.models import PeriodicTask, CrontabSchedule
        except ImportError as exc:
            logger.error("django-celery-beat not installed: %s", exc)
            return {
                "status": "error",
                "message": "django-celery-beat not available in this Dispatcharr build.",
            }

        schedule, _ = CrontabSchedule.objects.get_or_create(
            minute=minute, hour=hour, day_of_month=dom,
            month_of_year=month, day_of_week=dow,
        )

        # Snapshot the user-visible settings (drop any internal/private keys).
        snapshot = {k: v for k, v in (settings or {}).items() if not k.startswith("_")}

        _, created = PeriodicTask.objects.update_or_create(
            name=self.SCHEDULE_TASK_NAME,
            defaults={
                "crontab": schedule,
                "task": self.SCHEDULED_TASK_CELERY_NAME,
                "kwargs": json.dumps({"settings": snapshot}),
                "enabled": bool(settings.get("report_enabled", False)),
                "description": f"Daily report for {self.name} v{self.version}",
            },
        )
        verb = "Created" if created else "Updated"
        enabled = bool(settings.get("report_enabled", False))
        logger.info(
            "apply_schedule: %s '%s' @ '%s' enabled=%s",
            verb, self.SCHEDULE_TASK_NAME, cron_expr, enabled,
        )

        if not enabled:
            return {
                "status": "ok",
                "message": f"{verb} schedule for cron '{cron_expr}'. Enable 'Daily Report' setting + re-Apply to activate.",
            }
        return {
            "status": "ok",
            "message": f"{verb} schedule for cron '{cron_expr}'. Active.",
        }

    def _action_remove_schedule(self, settings: Dict[str, Any], logger) -> Dict[str, Any]:
        try:
            from django_celery_beat.models import PeriodicTask
        except ImportError:
            return {"status": "ok", "message": "django-celery-beat not installed; nothing to remove."}
        deleted, _ = PeriodicTask.objects.filter(name=self.SCHEDULE_TASK_NAME).delete()
        if deleted:
            logger.info("remove_schedule: deleted '%s'", self.SCHEDULE_TASK_NAME)
            return {"status": "ok", "message": "Schedule removed."}
        return {"status": "ok", "message": "No schedule was registered."}

    def _action_schedule_status(self, settings: Dict[str, Any], logger) -> Dict[str, Any]:
        try:
            from django_celery_beat.models import PeriodicTask
        except ImportError:
            return {"status": "ok", "message": "django-celery-beat not installed."}
        task = PeriodicTask.objects.filter(name=self.SCHEDULE_TASK_NAME).first()
        if not task:
            return {"status": "ok", "message": "No schedule registered. Click Apply to create one."}
        cron = task.crontab
        cron_expr = f"{cron.minute} {cron.hour} {cron.day_of_month} {cron.month_of_year} {cron.day_of_week}"
        last_run = task.last_run_at.isoformat() if task.last_run_at else "never"
        return {
            "status": "ok",
            "message": (
                f"cron='{cron_expr}' enabled={task.enabled} total_runs={task.total_run_count} "
                f"last_run={last_run}"
            ),
        }

    # ----- State file -----------------------------------------------------

    @staticmethod
    def _load_plugin_state() -> Dict[str, Any]:
        """Load persistent plugin state (last_report_at etc.) from a JSON
        file alongside the plugin code. Returns {} on any failure."""
        try:
            with open(PLUGIN_STATE_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:
            return {}

    @staticmethod
    def _save_plugin_state(state: Dict[str, Any]) -> None:
        """Write state atomically. Failure is non-fatal — losing state means
        next report falls back to the default 24h window."""
        try:
            tmp = PLUGIN_STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            _os.replace(tmp, PLUGIN_STATE_FILE)
        except Exception:
            pass

    @staticmethod
    def _parse_iso(value: Optional[str]) -> Optional[_dt.datetime]:
        if not value:
            return None
        try:
            dt = _dt.datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            return dt
        except Exception:
            return None

    # ----- Cron parsing ---------------------------------------------------

    @staticmethod
    def _parse_cron(expr: str) -> Tuple[str, str, str, str, str]:
        """Validate a 5-field cron expression and return its parts. Does not
        evaluate cron semantics — just structural validation. Same shape
        django-celery-beat expects."""
        parts = (expr or "").strip().split()
        if len(parts) != 5:
            raise ValueError(f"Cron expression must have 5 fields, got {len(parts)}: {expr!r}")
        return parts[0], parts[1], parts[2], parts[3], parts[4]

    # ----- Network helpers (not unit-tested — network-dependent) ---------

    @staticmethod
    def _http_get_json(url: str, timeout: int = REPORT_HTTP_TIMEOUT_SECS) -> Optional[Dict[str, Any]]:
        """Best-effort JSON GET. Returns None on any failure."""
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "telegram-alerts/0.4"})
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body) if body else None
        except Exception:
            return None

    @classmethod
    def _lookup_public_ip(cls) -> Optional[str]:
        data = cls._http_get_json(IPIFY_URL)
        if isinstance(data, dict):
            ip = data.get("ip")
            if isinstance(ip, str) and ip:
                return ip
        return None

    @classmethod
    def _lookup_geo(cls, ip: str) -> Optional[Dict[str, str]]:
        if not ip:
            return None
        data = cls._http_get_json(IPAPI_TEMPLATE.format(ip=urllib.parse.quote(ip)))
        if not isinstance(data, dict) or data.get("error"):
            return None
        # ipapi.co returns city, region, country_name, org (ISP), etc.
        return {
            "city": (data.get("city") or "").strip() or None,
            "region": (data.get("region") or "").strip() or None,
            "country": (data.get("country_name") or "").strip() or None,
            "isp": (data.get("org") or "").strip() or None,
        }

    @staticmethod
    def _speedtest_download() -> Optional[float]:
        """Download SPEEDTEST_DOWN_BYTES from Cloudflare and return Mbps.
        Returns None on any failure."""
        url = SPEEDTEST_DOWN_URL.format(bytes=SPEEDTEST_DOWN_BYTES)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "telegram-alerts/0.4"})
            t0 = _time.monotonic()
            with urllib.request.urlopen(request, timeout=SPEEDTEST_TIMEOUT_SECS) as resp:
                total = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
            elapsed = _time.monotonic() - t0
            if elapsed <= 0 or total <= 0:
                return None
            mbps = (total * 8) / elapsed / 1_000_000
            return round(mbps, 1)
        except Exception:
            return None

    @staticmethod
    def _speedtest_upload() -> Optional[float]:
        """POST SPEEDTEST_UP_BYTES to Cloudflare and return Mbps.
        Returns None on any failure."""
        try:
            payload = b"\0" * SPEEDTEST_UP_BYTES
            request = urllib.request.Request(
                SPEEDTEST_UP_URL,
                data=payload,
                headers={
                    "Content-Type": "application/octet-stream",
                    "User-Agent": "telegram-alerts/0.4",
                },
                method="POST",
            )
            t0 = _time.monotonic()
            with urllib.request.urlopen(request, timeout=SPEEDTEST_TIMEOUT_SECS) as resp:
                resp.read()  # drain
            elapsed = _time.monotonic() - t0
            if elapsed <= 0:
                return None
            mbps = (SPEEDTEST_UP_BYTES * 8) / elapsed / 1_000_000
            return round(mbps, 1)
        except Exception:
            return None

    # ----- Stats collection (DB-dependent, not unit-tested) --------------

    @staticmethod
    def _collect_activity_stats(window_start: _dt.datetime) -> Dict[str, Any]:
        """Aggregate SystemEvent counts since `window_start`. Returns a dict
        with channel_plays / top_channels / vod_plays / errors / switches.
        Empty dict on any failure."""
        out: Dict[str, Any] = {}
        try:
            from core.models import SystemEvent
            from django.db.models import Count
            qs = SystemEvent.objects.filter(created_at__gte=window_start)
            out["channel_plays"] = qs.filter(event_type="channel_start").count()
            out["vod_plays"] = qs.filter(event_type="vod_start").count()
            out["switches"] = qs.filter(event_type="stream_switch").count()
            out["errors"] = qs.filter(
                event_type__in=("channel_error", "channel_failover")
            ).count()
            top = (
                qs.filter(event_type="channel_start")
                .exclude(channel_name__isnull=True)
                .exclude(channel_name__exact="")
                .values("channel_name")
                .annotate(n=Count("id"))
                .order_by("-n")[:3]
            )
            out["top_channels"] = [(row["channel_name"], row["n"]) for row in top]
        except Exception:
            return {}
        return out

    @staticmethod
    def _collect_source_health() -> Dict[str, Any]:
        """Snapshot M3U + EPG source health. Empty dict on any failure."""
        out: Dict[str, Any] = {}
        try:
            from apps.m3u.models import M3UAccount
            accounts = list(M3UAccount.objects.all())
            out["m3u_total"] = len(accounts)
            # We don't know the exact field name for "is_active" across versions —
            # show the count and let users notice if something looks wrong.
            out["m3u_enabled"] = sum(1 for a in accounts if getattr(a, "is_active", True))
        except Exception:
            pass
        try:
            from apps.epg.models import EPGSource
            srcs = list(EPGSource.objects.all())
            out["epg_total"] = len(srcs)
            # Find most recent update timestamp across all sources.
            most_recent = None
            for s in srcs:
                ts = getattr(s, "updated_at", None) or getattr(s, "last_updated", None)
                if ts and (most_recent is None or ts > most_recent):
                    most_recent = ts
            out["epg_last_refresh"] = most_recent
        except Exception:
            pass
        return out

    # ----- Build + format report -----------------------------------------

    @classmethod
    def _build_report(
        cls,
        settings: Dict[str, Any],
        window_start: _dt.datetime,
        now: _dt.datetime,
        is_first_report: bool,
        logger,
    ) -> Dict[str, Any]:
        """Assemble a dict describing the report. The formatter consumes
        this; tests can exercise the formatter directly without network or
        DB access."""
        report: Dict[str, Any] = {
            "now": now,
            "window_start": window_start,
            "is_first_report": is_first_report,
            "speedtest_ran": False,
        }

        if settings.get("report_include_network"):
            ip = cls._lookup_public_ip()
            report["public_ip"] = ip
            report["geo"] = cls._lookup_geo(ip) if ip else None

            # Speedtest is gated by its own toggle AND a cooldown.
            if settings.get("report_include_speedtest"):
                state = cls._load_plugin_state()
                last_st = cls._parse_iso(state.get("last_speedtest_at"))
                cooldown_h = float(settings.get("report_speedtest_cooldown_hours") or 6)
                may_run = (
                    last_st is None
                    or (now - last_st) >= _dt.timedelta(hours=cooldown_h)
                )
                if may_run:
                    logger.info("report: running speedtest")
                    report["speedtest_down_mbps"] = cls._speedtest_download()
                    report["speedtest_up_mbps"] = cls._speedtest_upload()
                    report["speedtest_ran"] = True
                else:
                    logger.info(
                        "report: speedtest skipped (cooldown %.1fh, last %s)",
                        cooldown_h, last_st.isoformat() if last_st else "n/a",
                    )
                    report["speedtest_skipped_reason"] = (
                        f"cooldown — last test {cls._format_duration(now - last_st)} ago"
                        if last_st else "cooldown"
                    )

        if settings.get("report_include_activity"):
            report["activity"] = cls._collect_activity_stats(window_start)

        if settings.get("report_include_sources"):
            report["sources"] = cls._collect_source_health()

        return report

    @classmethod
    def _format_report_message(
        cls, report: Dict[str, Any], instance_label: str, fmt: str
    ) -> str:
        now: _dt.datetime = report["now"]
        window_start: _dt.datetime = report["window_start"]
        is_first = bool(report.get("is_first_report"))
        window_label = cls._format_window_label(window_start, now, is_first)

        is_html = (fmt == "HTML")
        bold = lambda s: f"<b>{cls._escape_html(s)}</b>" if is_html else s
        code = lambda s: f"<code>{cls._escape_html(s)}</code>" if is_html else s

        ts = now.strftime("%Y-%m-%d %H:%M")
        lines: List[str] = [f"📊 {bold(f'[{instance_label}] Dispatcharr report — {ts}')}"]

        # Network section
        if "public_ip" in report:
            lines.append("")
            lines.append(f"🌐 {bold('Network')}")
            ip = report.get("public_ip")
            geo = report.get("geo") or {}
            if ip:
                loc_bits = [b for b in (geo.get("city"), geo.get("region"), geo.get("country")) if b]
                loc = ", ".join(loc_bits)
                isp = geo.get("isp")
                tail_parts = []
                if loc:
                    tail_parts.append(loc)
                if isp:
                    tail_parts.append(isp)
                tail = f" ({'; '.join(tail_parts)})" if tail_parts else ""
                lines.append(f"IP: {code(ip)}{tail}")
            else:
                lines.append("IP: (lookup failed)")
            if report.get("speedtest_ran"):
                down = report.get("speedtest_down_mbps")
                up = report.get("speedtest_up_mbps")
                down_str = f"{down} Mbps" if down is not None else "(failed)"
                up_str = f"{up} Mbps" if up is not None else "(failed)"
                lines.append(f"Down: {code(down_str)}  Up: {code(up_str)}")
            elif "speedtest_skipped_reason" in report:
                lines.append(f"Speedtest: skipped ({report['speedtest_skipped_reason']})")

        # Activity section
        if "activity" in report:
            lines.append("")
            lines.append(f"📺 {bold(f'Activity — {window_label}')}")
            act = report["activity"] or {}
            if not act:
                lines.append("(no data available)")
            else:
                cp = act.get("channel_plays", 0)
                vp = act.get("vod_plays", 0)
                er = act.get("errors", 0)
                sw = act.get("switches", 0)
                top = act.get("top_channels") or []
                if cp:
                    top_str = ", ".join(f"{code(name)} ({n})" for name, n in top) if top else ""
                    if top_str:
                        lines.append(f"{cp} channel plays · top: {top_str}")
                    else:
                        lines.append(f"{cp} channel plays")
                else:
                    lines.append("0 channel plays")
                lines.append(f"{vp} VOD plays")
                if sw:
                    lines.append(f"{sw} stream switches")
                if er:
                    lines.append(f"{er} errors / failovers")

        # Sources section
        if "sources" in report:
            lines.append("")
            lines.append(f"📡 {bold('Sources')}")
            src = report["sources"] or {}
            if "m3u_total" in src:
                total = src["m3u_total"]
                enabled = src.get("m3u_enabled", total)
                if total == 0:
                    lines.append("No M3U accounts configured")
                elif enabled == total:
                    lines.append(f"{total} M3U accounts (all enabled)")
                else:
                    lines.append(f"{total} M3U accounts ({enabled} enabled)")
            if "epg_total" in src:
                total = src["epg_total"]
                last = src.get("epg_last_refresh")
                if total == 0:
                    lines.append("No EPG sources configured")
                elif last:
                    age = cls._format_duration(now - last) if isinstance(last, _dt.datetime) else "n/a"
                    lines.append(f"{total} EPG sources · last refresh {age} ago")
                else:
                    lines.append(f"{total} EPG sources")

        return "\n".join(lines)

    @staticmethod
    def _format_window_label(
        window_start: _dt.datetime, now: _dt.datetime, is_first_report: bool
    ) -> str:
        delta = now - window_start
        secs = max(0, int(delta.total_seconds()))
        if secs < 60 * 90:  # under 90 min
            label = f"last {max(1, secs // 60)} min"
        elif secs < 60 * 60 * 36:  # under 36 hours
            label = f"last {round(secs / 3600)}h"
        elif secs < 60 * 60 * 24 * 14:  # under 14 days
            label = f"last {round(secs / 86400)} days"
        else:
            weeks = round(secs / (86400 * 7))
            label = f"last {weeks} weeks"
        if is_first_report:
            label += " (first report)"
        return label

    @staticmethod
    def _format_duration(delta: _dt.timedelta) -> str:
        secs = max(0, int(delta.total_seconds()))
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m"
        if secs < 86400:
            return f"{secs // 3600}h"
        return f"{secs // 86400}d"


# ===== Module-level: Celery task registration ============================
#
# Wrapped in try/except so the plugin still loads on Dispatcharr builds where
# Celery isn't importable (e.g. test environments). Without Celery the
# scheduled report won't fire, but the manual Send Report button still works.

try:
    from celery import shared_task as _telegram_alerts_shared_task

    @_telegram_alerts_shared_task(name=Plugin.SCHEDULED_TASK_CELERY_NAME)
    def _telegram_alerts_send_daily_report(settings=None):
        """Celery entry point invoked by the periodic task registered via
        _action_apply_schedule. Runs `send_report_now` against the snapshot
        settings stored in PeriodicTask.kwargs."""
        import logging as _logging
        logger = _logging.getLogger("telegram_alerts.schedule")
        return Plugin().run("send_report_now", {}, {
            "logger": logger,
            "settings": settings or {},
        })
except Exception as _celery_register_err:  # pragma: no cover
    import sys as _sys
    print(
        f"[telegram_alerts] Celery task registration failed: {_celery_register_err}",
        file=_sys.stderr,
    )
