"""Unit tests for Telegram Alerts pure helpers.

These methods don't touch the network, Django, or the filesystem. The
network helper `_send_telegram` is exercised in production via the
'Send Test' action and not unit-tested here.

Run with `pytest` from the repo root.
"""
import os
import sys

# Make the repo root importable so `from plugin import Plugin` resolves.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from plugin import EVENT_META, EVENT_NAMES, Plugin


# ---------- _mask_token ------------------------------------------------------

class TestMaskToken:
    def test_empty_string(self):
        assert Plugin._mask_token("") == ""

    def test_none_treated_as_empty(self):
        assert Plugin._mask_token(None) == ""

    def test_no_colon_returns_full_redaction(self):
        assert Plugin._mask_token("notatoken") == "***"

    def test_keeps_bot_id_and_last4_of_secret(self):
        token = "123456789:ABCdefGhIJklmNOpqrsTUVwxyz"
        masked = Plugin._mask_token(token)
        assert masked == "123456789:***wxyz"

    def test_short_secret_fully_masked(self):
        # Real tokens are far longer than this, but be defensive.
        assert Plugin._mask_token("123:ab") == "123:***"

    def test_secret_never_appears_in_full(self):
        token = "999:supersecretstring"
        masked = Plugin._mask_token(token)
        assert "supersecretstring" not in masked
        assert "999:" in masked


# ---------- _validate_credentials --------------------------------------------

class TestValidateCredentials:
    def test_valid_returns_ok(self):
        ok, err = Plugin._validate_credentials("123456:abcdefghij1234567890", "123456789")
        assert ok is True
        assert err is None

    def test_valid_negative_chat_id_for_group(self):
        ok, err = Plugin._validate_credentials("123456:abcdefghij1234567890", "-1001234567890")
        assert ok is True
        assert err is None

    def test_missing_token_rejected(self):
        ok, err = Plugin._validate_credentials("", "123456789")
        assert ok is False
        assert "Bot Token" in err

    def test_malformed_token_no_colon(self):
        ok, err = Plugin._validate_credentials("nothiniswrongherejustlongenuf", "123456789")
        assert ok is False
        assert "malformed" in err.lower()

    def test_malformed_token_too_short(self):
        ok, err = Plugin._validate_credentials("a:b", "123456789")
        assert ok is False
        assert "malformed" in err.lower()

    def test_missing_chat_id_rejected(self):
        ok, err = Plugin._validate_credentials("123456:abcdefghij1234567890", "")
        assert ok is False
        assert "Chat ID" in err

    def test_non_numeric_chat_id_rejected(self):
        ok, err = Plugin._validate_credentials("123456:abcdefghij1234567890", "abc123")
        assert ok is False
        assert "numeric" in err.lower()

    def test_chat_id_with_whitespace_inside_rejected(self):
        # Settings code .strip()s outer whitespace; embedded whitespace must fail.
        ok, err = Plugin._validate_credentials("123456:abcdefghij1234567890", "12 34")
        assert ok is False


# ---------- _escape_html -----------------------------------------------------

class TestEscapeHtml:
    def test_none_becomes_empty(self):
        assert Plugin._escape_html(None) == ""

    def test_plain_text_passes_through(self):
        assert Plugin._escape_html("hello world") == "hello world"

    def test_lt_gt_escaped(self):
        assert Plugin._escape_html("a < b > c") == "a &lt; b &gt; c"

    def test_ampersand_escaped(self):
        assert Plugin._escape_html("Tom & Jerry") == "Tom &amp; Jerry"

    def test_quotes_not_escaped(self):
        # Telegram HTML doesn't require quote escaping in text content;
        # `quote=False` keeps messages readable.
        assert Plugin._escape_html('say "hi"') == 'say "hi"'

    def test_non_string_coerced(self):
        assert Plugin._escape_html(42) == "42"


# ---------- _format_test_message --------------------------------------------

class TestFormatTestMessage:
    def test_html_includes_label(self):
        msg = Plugin._format_test_message("Yoda", "HTML")
        assert "<b>" in msg
        assert "[Yoda]" in msg
        assert "✅" in msg

    def test_plain_has_no_tags(self):
        msg = Plugin._format_test_message("Yoda", "plain")
        assert "<b>" not in msg
        assert "[Yoda]" in msg

    def test_html_escapes_label(self):
        msg = Plugin._format_test_message("A <prod> & B", "HTML")
        assert "&lt;prod&gt;" in msg
        assert "&amp;" in msg

    def test_plain_does_not_escape_label(self):
        # Plain mode just passes the label through; the user gets what they typed.
        msg = Plugin._format_test_message("A <prod> & B", "plain")
        assert "A <prod> & B" in msg


# ---------- _format_event_message -------------------------------------------

class TestFormatEventMessage:
    def test_html_channel_start(self):
        msg = Plugin._format_event_message(
            "channel_start",
            {"channel_name": "ESPN"},
            "Yoda",
            "HTML",
        )
        assert "▶" in msg
        assert "Channel started" in msg
        assert "[Yoda]" in msg
        assert "<code>ESPN</code>" in msg
        assert "Stream:" not in msg  # no stream_name in payload

    def test_html_stream_switch_includes_stream(self):
        msg = Plugin._format_event_message(
            "stream_switch",
            {"channel_name": "ESPN", "stream_name": "backup-feed"},
            "Yoda",
            "HTML",
        )
        assert "🔀" in msg
        assert "<code>backup-feed</code>" in msg

    def test_plain_format_strips_tags(self):
        msg = Plugin._format_event_message(
            "channel_reconnect",
            {"channel_name": "ESPN"},
            "Yoda",
            "plain",
        )
        assert "<b>" not in msg
        assert "<code>" not in msg
        assert "🔄" in msg
        assert "ESPN" in msg

    def test_missing_channel_name_falls_back(self):
        msg = Plugin._format_event_message(
            "channel_start",
            {},
            "Yoda",
            "HTML",
        )
        assert "(unknown)" in msg

    def test_html_escapes_payload(self):
        # Channel name contains HTML — must be escaped to avoid Telegram parse errors.
        msg = Plugin._format_event_message(
            "channel_start",
            {"channel_name": "Sports & <stuff>"},
            "Yoda",
            "HTML",
        )
        assert "Sports &amp; &lt;stuff&gt;" in msg
        assert "<stuff>" not in msg

    def test_html_escapes_instance_label(self):
        msg = Plugin._format_event_message(
            "channel_start",
            {"channel_name": "ESPN"},
            "<prod>",
            "HTML",
        )
        assert "&lt;prod&gt;" in msg

    def test_unknown_event_uses_fallback_label(self):
        # Defensive: if Dispatcharr ever emits a new event we haven't mapped,
        # the formatter shouldn't crash.
        msg = Plugin._format_event_message(
            "some_new_event",
            {"channel_name": "ESPN"},
            "Yoda",
            "HTML",
        )
        assert "ESPN" in msg
        assert "some_new_event" in msg


# ---------- EVENT_META consistency ------------------------------------------

class TestEventMetaConsistency:
    """Catch refactor mistakes: every subscribed event needs metadata, and
    every toggle referenced in metadata must exist as a settings field."""

    def test_event_names_all_have_metadata(self):
        for name in EVENT_NAMES:
            assert name in EVENT_META, f"Missing EVENT_META entry for {name!r}"

    def test_metadata_references_real_setting_fields(self):
        field_ids = {f["id"] for f in Plugin.fields}
        for name, meta in EVENT_META.items():
            assert meta["toggle"] in field_ids, (
                f"EVENT_META[{name!r}].toggle = {meta['toggle']!r} "
                f"is not a defined settings field"
            )

    def test_action_events_match_event_names(self):
        on_event = next(a for a in Plugin.actions if a["id"] == "on_event")
        assert set(on_event["events"]) == set(EVENT_NAMES)


# ---------- Plugin manifest sanity ------------------------------------------

class TestManifest:
    def test_no_checkbox_fields(self):
        # Dispatcharr silently drops checkbox fields. Use 'boolean' instead.
        for field in Plugin.fields:
            assert field["type"] != "checkbox", (
                f"Field {field['id']!r} uses 'checkbox' which Dispatcharr drops; "
                "use 'boolean'."
            )

    def test_action_descriptions_are_one_line(self):
        # Multi-line action descriptions cause the Run button to wrap below
        # the title in the Dispatcharr UI.
        for action in Plugin.actions:
            assert "\n" not in action["description"], (
                f"Action {action['id']!r} description contains a newline"
            )

    def test_send_test_has_button(self):
        send_test = next(a for a in Plugin.actions if a["id"] == "send_test")
        assert "button_label" in send_test

    def test_on_event_has_no_button(self):
        # Internal/event-only actions intentionally omit button_label so
        # Dispatcharr doesn't render them as clickable.
        on_event = next(a for a in Plugin.actions if a["id"] == "on_event")
        assert "button_label" not in on_event
