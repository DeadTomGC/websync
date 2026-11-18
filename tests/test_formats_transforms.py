"""Tests for text format converters and value transforms."""

from __future__ import annotations

import pytest

from websync.formats import convert_text
from websync.transforms import apply_transform, known_transforms


class TestConvertText:
    def test_passthrough_on_same_format(self):
        assert convert_text("<b>x</b>", "html", "html") == "<b>x</b>"

    def test_passthrough_on_non_string(self):
        assert convert_text(5, "plain", "html") == 5

    def test_html_to_plain_strips_tags(self):
        assert convert_text("<p>Hello <b>world</b>!</p>", "html", "plain") == "Hello world!"

    def test_html_to_plain_unescapes_entities(self):
        assert convert_text("<p>a &amp; b</p>", "html", "plain") == "a & b"

    def test_html_to_plain_drops_script(self):
        assert convert_text("<p>ok</p><script>alert(1)</script>", "html", "plain") == "ok"

    def test_plain_to_html_escapes(self):
        out = convert_text("a < b & c", "plain", "html")
        assert "a &lt; b &amp; c" in out

    def test_markdown_to_plain(self):
        assert (
            convert_text("# Title\n\n**bold** and *it*", "markdown", "plain")
            == "Title\nbold and it"
        )

    def test_html_to_markdown_via_plain(self):
        out = convert_text("<p>hi</p>", "html", "markdown")
        assert out == "hi"

    def test_unknown_format_raises(self):
        with pytest.raises(ValueError):
            convert_text("x", "rtf", "plain")

    def test_plain_roundtrip_through_html(self):
        text = "line one\nline two"
        back = convert_text(convert_text(text, "plain", "html"), "html", "plain")
        assert back == text


class TestTransforms:
    def test_man_days_to_points(self):
        assert apply_transform("man_days_to_points", 3) == 6
        assert apply_transform("man_days_to_points", "2.5") == 5

    def test_none_passes_through(self):
        assert apply_transform("man_days_to_points", None) is None

    def test_unknown_transform_raises(self):
        with pytest.raises(KeyError):
            apply_transform("nope", 1)

    def test_registry_listed(self):
        assert "man_days_to_points" in known_transforms()
