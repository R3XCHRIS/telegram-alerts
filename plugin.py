"""
Telegram Alerts — Dispatcharr plugin
(slug: telegram-alerts)
v0.2.1 — stream source respects channel priority order (was previously
         returning whatever Stream PK came first, not the user's #1).

MIT License
Copyright (c) 2026 R3XCHRIS
https://github.com/R3XCHRIS/telegram-alerts
"""
import html as _html
import json
import logging
import re
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple


# Events Dispatcharr emits that this plugin subscribes to. Names match the
# static-image-detector reference plugin and Dispatcharr's
# `dispatch_event_system`. The payload Dispatcharr passes via
# `params["payload"]` is a dict containing at least `channel_name`; for
# stream_switch it also carries `stream_name`.
EVENT_NAMES = ("channel_start", "channel_stop", "channel_reconnect", "stream_switch")

# Per-event presentation. Order: emoji, severity label, default-on flag.
# `default_on` is documented here so the fields list stays the source of truth.
EVENT_META = {
    "channel_start":     {"emoji": "▶",  "label": "Channel started",     "toggle": "alert_channel_start"},
    "channel_stop":      {"emoji": "⏹",  "label": "Channel stopped",     "toggle": "alert_channel_stop"},
    "channel_reconnect": {"emoji": "🔄", "label": "Channel reconnected", "toggle": "alert_channel_reconnect"},
    "stream_switch":     {"emoji": "🔀", "label": "Stream switched",     "toggle": "alert_stream_switch"},
}

TELEGRAM_API = "https://api.telegram.org"
HTTP_TIMEOUT_SECS = 10


class Plugin:
    """Send Dispatcharr alerts to a Telegram chat."""

    name = "Telegram Alerts"
    version = "0.2.1"
    description = (
        "Push Dispatcharr channel/stream events to a Telegram chat via a bot. "
        "Includes a manual test action and per-event toggles."
    )

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
            "id": "on_event",
            "label": "Handle channel/stream event (internal)",
            "description": "Triggered by Dispatcharr on channel/stream events. Don't run manually.",
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

        channel_name = payload.get("channel_name")
        source = (
            self._lookup_stream_source(channel_name)
            if settings.get("include_stream_source")
            else None
        )
        program = (
            self._lookup_current_program(channel_name)
            if settings.get("include_current_program")
            else None
        )

        text = self._format_event_message(
            event, payload, label, fmt, source=source, program=program,
        )
        logger.info(
            "on_event[%s]: channel=%s source=%s program=%s sending to chat=%s",
            event, channel_name, source, program, chat_id,
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
        channel = payload.get("channel_name") or "(unknown)"
        stream = payload.get("stream_name")

        if fmt == "HTML":
            lines = [
                f"{emoji} <b>[{cls._escape_html(instance_label)}] {cls._escape_html(label)}</b>",
                f"Channel: <code>{cls._escape_html(channel)}</code>",
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
            f"Channel: {channel}",
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
