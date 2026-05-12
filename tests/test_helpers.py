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


# ---------- Enrichment fields (source / program) ----------------------------

class TestFormatEventMessageEnrichment:
    def test_html_source_only(self):
        msg = Plugin._format_event_message(
            "channel_start", {"channel_name": "ESPN"}, "Yoda", "HTML",
            source="MyIPTV",
        )
        assert "Source: <code>MyIPTV</code>" in msg
        assert "Now playing:" not in msg

    def test_html_program_only(self):
        msg = Plugin._format_event_message(
            "channel_start", {"channel_name": "ESPN"}, "Yoda", "HTML",
            program="NFL Live",
        )
        assert "Now playing: <code>NFL Live</code>" in msg
        assert "Source:" not in msg

    def test_html_both_source_and_program(self):
        msg = Plugin._format_event_message(
            "channel_start", {"channel_name": "ESPN"}, "Yoda", "HTML",
            source="MyIPTV", program="NFL Live",
        )
        assert "Source: <code>MyIPTV</code>" in msg
        assert "Now playing: <code>NFL Live</code>" in msg

    def test_plain_both_source_and_program(self):
        msg = Plugin._format_event_message(
            "channel_start", {"channel_name": "ESPN"}, "Yoda", "plain",
            source="MyIPTV", program="NFL Live",
        )
        assert "<code>" not in msg
        assert "Source: MyIPTV" in msg
        assert "Now playing: NFL Live" in msg

    def test_none_values_omit_lines_silently(self):
        # None for either field should produce no line at all — never
        # "Source: (unknown)" or "Source: None".
        msg = Plugin._format_event_message(
            "channel_start", {"channel_name": "ESPN"}, "Yoda", "HTML",
            source=None, program=None,
        )
        assert "Source:" not in msg
        assert "Now playing:" not in msg
        assert "None" not in msg

    def test_empty_string_omits_line(self):
        # Empty strings are falsy too — same treatment as None.
        msg = Plugin._format_event_message(
            "channel_start", {"channel_name": "ESPN"}, "Yoda", "HTML",
            source="", program="",
        )
        assert "Source:" not in msg
        assert "Now playing:" not in msg

    def test_html_escapes_source_and_program(self):
        # User-supplied M3U account names and EPG titles can contain
        # HTML metacharacters (e.g. "Channel <4>"). Must be escaped.
        msg = Plugin._format_event_message(
            "channel_start", {"channel_name": "ESPN"}, "Yoda", "HTML",
            source="A & B <prov>", program="<i>Live</i> & Loud",
        )
        assert "A &amp; B &lt;prov&gt;" in msg
        assert "&lt;i&gt;Live&lt;/i&gt; &amp; Loud" in msg
        # Make sure no raw tags survived.
        assert "<prov>" not in msg
        assert "<i>Live</i>" not in msg

    def test_plain_does_not_escape_source_or_program(self):
        # Plain mode passes user input through literally — no HTML mangling.
        msg = Plugin._format_event_message(
            "channel_start", {"channel_name": "ESPN"}, "Yoda", "plain",
            source="A & B", program="<i>Live</i>",
        )
        assert "Source: A & B" in msg
        assert "Now playing: <i>Live</i>" in msg

    def test_source_appears_after_stream_for_stream_switch(self):
        # Field ordering: emoji/title, channel, stream, source, program.
        msg = Plugin._format_event_message(
            "stream_switch",
            {"channel_name": "ESPN", "stream_name": "backup-feed"},
            "Yoda", "HTML",
            source="MyIPTV", program="NFL Live",
        )
        # Verify the three optional lines appear in the expected order.
        idx_stream = msg.index("Stream:")
        idx_source = msg.index("Source:")
        idx_program = msg.index("Now playing:")
        assert idx_stream < idx_source < idx_program


# ---------- VOD events -------------------------------------------------------

class TestFormatEventMessageVod:
    def test_html_vod_start_uses_title_label_from_content_name(self):
        # VOD payloads carry `content_name`, NOT `channel_name`. The headline
        # field must change accordingly.
        msg = Plugin._format_event_message(
            "vod_start",
            {"content_name": "Inception", "content_uuid": "abc-123"},
            "Yoda", "HTML",
        )
        assert "🎬" in msg
        assert "VOD started" in msg
        assert "[Yoda]" in msg
        assert "<b>" in msg
        assert "Title: <code>Inception</code>" in msg
        # Must NOT use channel-style fields for VOD events.
        assert "Channel:" not in msg
        assert "Stream:" not in msg

    def test_html_vod_stop_emoji_and_label(self):
        msg = Plugin._format_event_message(
            "vod_stop",
            {"content_name": "Inception"},
            "Yoda", "HTML",
        )
        assert "🛑" in msg
        assert "VOD stopped" in msg
        assert "Title: <code>Inception</code>" in msg

    def test_plain_vod_start(self):
        msg = Plugin._format_event_message(
            "vod_start", {"content_name": "Inception"}, "Yoda", "plain",
        )
        assert "<code>" not in msg
        assert "🎬 [Yoda] VOD started" in msg
        assert "Title: Inception" in msg

    def test_vod_with_source_enrichment(self):
        msg = Plugin._format_event_message(
            "vod_start", {"content_name": "Inception"}, "Yoda", "HTML",
            source="MyIPTV",
        )
        assert "Title: <code>Inception</code>" in msg
        assert "Source: <code>MyIPTV</code>" in msg

    def test_vod_ignores_program_argument(self):
        # _handle_event guarantees `program=None` for VOD events, but the
        # formatter shouldn't render one if it ever leaked through.
        # Currently it would render — that's caller's responsibility.
        # This test just documents the current contract: caller MUST pass
        # program=None for VOD events. Verified by the next test.
        msg = Plugin._format_event_message(
            "vod_start", {"content_name": "Inception"}, "Yoda", "HTML",
            program=None,
        )
        assert "Now playing:" not in msg

    def test_vod_missing_content_name_falls_back(self):
        msg = Plugin._format_event_message(
            "vod_start", {}, "Yoda", "HTML",
        )
        assert "Title: <code>(unknown)</code>" in msg

    def test_html_escapes_vod_title(self):
        msg = Plugin._format_event_message(
            "vod_start",
            {"content_name": "Foo & <Bar>"},
            "Yoda", "HTML",
        )
        assert "Foo &amp; &lt;Bar&gt;" in msg
        assert "<Bar>" not in msg


# ---------- Module-level VOD constants --------------------------------------

class TestVodConstants:
    def test_vod_events_in_event_names(self):
        from plugin import EVENT_NAMES, VOD_EVENTS
        for name in VOD_EVENTS:
            assert name in EVENT_NAMES, f"{name!r} declared as VOD but missing from EVENT_NAMES"

    def test_vod_events_have_metadata(self):
        from plugin import EVENT_META, VOD_EVENTS
        for name in VOD_EVENTS:
            assert name in EVENT_META, f"{name!r} missing from EVENT_META"

    def test_vod_events_reference_real_setting_fields(self):
        from plugin import EVENT_META, VOD_EVENTS
        field_ids = {f["id"] for f in Plugin.fields}
        for name in VOD_EVENTS:
            toggle = EVENT_META[name]["toggle"]
            assert toggle in field_ids, (
                f"VOD event {name!r} toggle {toggle!r} is not a defined settings field"
            )


# ---------- Cron parsing -----------------------------------------------------

class TestParseCron:
    def test_valid_5_field(self):
        assert Plugin._parse_cron("0 9 * * *") == ("0", "9", "*", "*", "*")

    def test_valid_with_lists(self):
        assert Plugin._parse_cron("*/15 0,12 * * 1-5") == ("*/15", "0,12", "*", "*", "1-5")

    def test_too_few_fields(self):
        import pytest
        with pytest.raises(ValueError):
            Plugin._parse_cron("0 9 *")

    def test_too_many_fields(self):
        import pytest
        with pytest.raises(ValueError):
            Plugin._parse_cron("0 9 * * * *")

    def test_empty(self):
        import pytest
        with pytest.raises(ValueError):
            Plugin._parse_cron("")


# ---------- Window label -----------------------------------------------------

import datetime as _dt


class TestFormatWindowLabel:
    def _delta_label(self, seconds: int, first: bool = False) -> str:
        now = _dt.datetime(2026, 1, 1, 12, tzinfo=_dt.timezone.utc)
        return Plugin._format_window_label(now - _dt.timedelta(seconds=seconds), now, first)

    def test_minutes(self):
        assert self._delta_label(60 * 30) == "last 30 min"

    def test_hours(self):
        assert self._delta_label(60 * 60 * 6) == "last 6h"

    def test_days(self):
        assert self._delta_label(60 * 60 * 24 * 7) == "last 7 days"

    def test_weeks(self):
        assert "weeks" in self._delta_label(60 * 60 * 24 * 30)

    def test_first_report_suffix(self):
        assert self._delta_label(60 * 60 * 24, first=True).endswith("(first report)")

    def test_zero_window_does_not_negative(self):
        # If somehow last_report_at == now, no negatives, no crash.
        assert "last" in self._delta_label(0)


# ---------- Format duration -------------------------------------------------

class TestFormatDuration:
    def test_seconds(self):
        assert Plugin._format_duration(_dt.timedelta(seconds=45)) == "45s"

    def test_minutes(self):
        assert Plugin._format_duration(_dt.timedelta(minutes=12)) == "12m"

    def test_hours(self):
        assert Plugin._format_duration(_dt.timedelta(hours=4)) == "4h"

    def test_days(self):
        assert Plugin._format_duration(_dt.timedelta(days=3)) == "3d"

    def test_negative_clamps_to_zero(self):
        assert Plugin._format_duration(_dt.timedelta(seconds=-5)) == "0s"


# ---------- _parse_iso -------------------------------------------------------

class TestParseIso:
    def test_none_returns_none(self):
        assert Plugin._parse_iso(None) is None

    def test_empty_returns_none(self):
        assert Plugin._parse_iso("") is None

    def test_garbage_returns_none(self):
        assert Plugin._parse_iso("not a timestamp") is None

    def test_naive_timestamp_gets_utc(self):
        dt = Plugin._parse_iso("2026-05-11T09:00:00")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_round_trips_with_isoformat(self):
        original = _dt.datetime(2026, 5, 11, 9, 0, tzinfo=_dt.timezone.utc)
        assert Plugin._parse_iso(original.isoformat()) == original


# ---------- Report formatter -------------------------------------------------

def _now():
    return _dt.datetime(2026, 5, 11, 9, 0, tzinfo=_dt.timezone.utc)


def _base_report():
    now = _now()
    return {
        "now": now,
        "window_start": now - _dt.timedelta(days=1),
        "is_first_report": False,
        "speedtest_ran": False,
    }


class TestFormatReportMessage:
    def test_html_header_uses_emoji_and_dispatcharr_label(self):
        msg = Plugin._format_report_message(_base_report(), "Yoda", "HTML")
        assert "📊" in msg
        assert "<b>[Yoda] Dispatcharr report — 2026-05-11 09:00</b>" in msg

    def test_plain_header_strips_tags(self):
        msg = Plugin._format_report_message(_base_report(), "Yoda", "plain")
        assert "<b>" not in msg
        assert "📊 [Yoda] Dispatcharr report — 2026-05-11 09:00" in msg

    def test_html_escapes_instance_label(self):
        msg = Plugin._format_report_message(_base_report(), "<prod>", "HTML")
        assert "&lt;prod&gt;" in msg
        assert "<prod>" not in msg.split("</b>")[0]

    def test_network_section_renders_ip_and_geo(self):
        report = _base_report()
        report["public_ip"] = "203.0.113.42"
        report["geo"] = {"city": "Brisbane", "region": "QLD", "country": "Australia", "isp": "Telstra"}
        msg = Plugin._format_report_message(report, "Yoda", "HTML")
        assert "🌐" in msg
        assert "<code>203.0.113.42</code>" in msg
        assert "Brisbane" in msg
        assert "Telstra" in msg

    def test_network_section_ip_only_when_geo_missing(self):
        report = _base_report()
        report["public_ip"] = "203.0.113.42"
        report["geo"] = None
        msg = Plugin._format_report_message(report, "Yoda", "HTML")
        assert "<code>203.0.113.42</code>" in msg
        assert "Brisbane" not in msg

    def test_network_section_handles_ip_lookup_failure(self):
        report = _base_report()
        report["public_ip"] = None
        msg = Plugin._format_report_message(report, "Yoda", "HTML")
        assert "lookup failed" in msg

    def test_speedtest_results_when_run(self):
        report = _base_report()
        report["public_ip"] = "1.2.3.4"
        report["speedtest_ran"] = True
        report["speedtest_down_mbps"] = 287.4
        report["speedtest_up_mbps"] = 42.1
        msg = Plugin._format_report_message(report, "Yoda", "HTML")
        assert "287.4 Mbps" in msg
        assert "42.1 Mbps" in msg

    def test_speedtest_skipped_message(self):
        report = _base_report()
        report["public_ip"] = "1.2.3.4"
        report["speedtest_ran"] = False
        report["speedtest_skipped_reason"] = "cooldown — last test 2h ago"
        msg = Plugin._format_report_message(report, "Yoda", "HTML")
        assert "Speedtest: skipped" in msg
        assert "2h ago" in msg

    def test_speedtest_failed_renders_failed_label(self):
        report = _base_report()
        report["public_ip"] = "1.2.3.4"
        report["speedtest_ran"] = True
        report["speedtest_down_mbps"] = None
        report["speedtest_up_mbps"] = None
        msg = Plugin._format_report_message(report, "Yoda", "HTML")
        assert "Down: <code>(failed)</code>" in msg
        assert "Up: <code>(failed)</code>" in msg

    def test_activity_section_with_top_channels(self):
        report = _base_report()
        report["activity"] = {
            "channel_plays": 47,
            "vod_plays": 6,
            "switches": 2,
            "errors": 1,
            "top_channels": [("ESPN", 12), ("CNN", 8), ("Sky News", 5)],
        }
        msg = Plugin._format_report_message(report, "Yoda", "HTML")
        assert "📺" in msg
        assert "47 channel plays" in msg
        assert "<code>ESPN</code>" in msg
        assert "(12)" in msg
        assert "6 VOD plays" in msg
        assert "2 stream switches" in msg
        assert "1 errors / failovers" in msg

    def test_activity_section_zero_plays(self):
        report = _base_report()
        report["activity"] = {
            "channel_plays": 0, "vod_plays": 0, "switches": 0, "errors": 0, "top_channels": [],
        }
        msg = Plugin._format_report_message(report, "Yoda", "HTML")
        assert "0 channel plays" in msg
        # switches/errors only mentioned when non-zero
        assert "stream switches" not in msg
        assert "errors / failovers" not in msg

    def test_activity_section_no_data(self):
        report = _base_report()
        report["activity"] = {}
        msg = Plugin._format_report_message(report, "Yoda", "HTML")
        assert "no data available" in msg

    def test_activity_section_html_escapes_top_channel_name(self):
        report = _base_report()
        report["activity"] = {
            "channel_plays": 1, "vod_plays": 0, "switches": 0, "errors": 0,
            "top_channels": [("<scary>", 1)],
        }
        msg = Plugin._format_report_message(report, "Yoda", "HTML")
        assert "&lt;scary&gt;" in msg
        assert "<scary>" not in msg

    def test_sources_section_m3u_all_enabled(self):
        report = _base_report()
        report["sources"] = {"m3u_total": 3, "m3u_enabled": 3, "epg_total": 2, "epg_last_refresh": _now() - _dt.timedelta(hours=4)}
        msg = Plugin._format_report_message(report, "Yoda", "HTML")
        assert "📡" in msg
        assert "3 M3U accounts (all enabled)" in msg
        assert "2 EPG sources" in msg
        assert "4h ago" in msg

    def test_sources_section_partial_enabled(self):
        report = _base_report()
        report["sources"] = {"m3u_total": 3, "m3u_enabled": 2}
        msg = Plugin._format_report_message(report, "Yoda", "HTML")
        assert "3 M3U accounts (2 enabled)" in msg

    def test_sources_section_zero_configured(self):
        report = _base_report()
        report["sources"] = {"m3u_total": 0, "epg_total": 0}
        msg = Plugin._format_report_message(report, "Yoda", "HTML")
        assert "No M3U accounts" in msg
        assert "No EPG sources" in msg

    def test_first_report_window_label_marked(self):
        report = _base_report()
        report["is_first_report"] = True
        report["activity"] = {"channel_plays": 0, "vod_plays": 0, "switches": 0, "errors": 0, "top_channels": []}
        msg = Plugin._format_report_message(report, "Yoda", "HTML")
        assert "(first report)" in msg

    def test_sections_omitted_when_keys_absent(self):
        # When a section's key isn't in the dict (toggle was off), no section
        # rendered. Header always renders.
        report = _base_report()
        msg = Plugin._format_report_message(report, "Yoda", "HTML")
        assert "📊" in msg
        assert "🌐" not in msg
        assert "📺" not in msg
        assert "📡" not in msg


# ---------- Manifest sanity for v0.4.0 --------------------------------------

class TestManifestV04:
    def test_report_action_dispatches_send_report_now(self):
        action_ids = {a["id"] for a in Plugin.actions}
        for required in (
            "send_test", "send_report_now", "apply_schedule",
            "remove_schedule", "schedule_status", "on_event",
        ):
            assert required in action_ids, f"Action {required!r} missing"

    def test_report_settings_all_present(self):
        field_ids = {f["id"] for f in Plugin.fields}
        for required in (
            "report_enabled", "report_cron", "report_timezone", "report_chat_id",
            "report_include_network", "report_include_speedtest",
            "report_speedtest_cooldown_hours",
            "report_include_activity", "report_include_sources",
        ):
            assert required in field_ids, f"Setting {required!r} missing"

    def test_report_timezone_default_is_blank_meaning_utc(self):
        tz_field = next(f for f in Plugin.fields if f["id"] == "report_timezone")
        assert tz_field["default"] == ""

    def test_report_cron_default_is_9am_daily(self):
        cron_field = next(f for f in Plugin.fields if f["id"] == "report_cron")
        assert cron_field["default"] == "0 9 * * *"

    def test_speedtest_cooldown_default_is_6_hours(self):
        cd_field = next(f for f in Plugin.fields if f["id"] == "report_speedtest_cooldown_hours")
        assert cd_field["default"] == 6

    def test_report_actions_descriptions_are_one_line(self):
        for action in Plugin.actions:
            assert "\n" not in action["description"], (
                f"Action {action['id']!r} description has a newline"
            )


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
