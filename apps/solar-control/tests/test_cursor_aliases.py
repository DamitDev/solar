"""Alias table tests for the /cursor/v1 model surface."""

from app.cursor_proxy.aliases import (
    CURSOR_ALIASES,
    DEFAULT_MAX_MODEL_LEN,
    UPSTREAM_MODEL,
    alias_model_entries,
    is_cursor_alias,
    reasoning_effort_for,
)


def test_alias_table_exposes_four_variants():
    assert set(CURSOR_ALIASES) == {
        "deepseek-v4-flash:max",
        "deepseek-v4-flash:high",
        "krumpli:max",
        "krumpli:high",
    }


def test_alias_effort_mapping():
    assert reasoning_effort_for("deepseek-v4-flash:max") == "max"
    assert reasoning_effort_for("deepseek-v4-flash:high") == "high"
    assert reasoning_effort_for("krumpli:max") == "max"
    assert reasoning_effort_for("krumpli:high") == "high"


def test_unknown_model_gets_default_effort():
    assert reasoning_effort_for("something:else", default="high") == "high"
    assert reasoning_effort_for("something:else") == "max"


def test_is_cursor_alias():
    assert is_cursor_alias("deepseek-v4-flash:max")
    assert is_cursor_alias("krumpli:high")
    assert not is_cursor_alias("deepseek-v4-flash:284b")
    assert not is_cursor_alias("deepseek-v4-pro")


def test_model_entries_advertise_all_aliases():
    entries = alias_model_entries()
    assert [entry["id"] for entry in entries] == list(CURSOR_ALIASES)
    for entry in entries:
        assert entry["object"] == "model"
        assert entry["root"] == UPSTREAM_MODEL
        assert entry["max_model_len"] == DEFAULT_MAX_MODEL_LEN


def test_model_entries_use_provided_context():
    entries = alias_model_entries(max_model_len=12345)
    assert all(entry["max_model_len"] == 12345 for entry in entries)
