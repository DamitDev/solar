"""Tests for llama.cpp command construction."""

from types import SimpleNamespace

from solar_host.backends.llamacpp import LlamaCppRunner
from solar_host.models.llamacpp import LlamaCppConfig


def build_command(**config_overrides: object) -> list[str]:
    config = LlamaCppConfig(model="/models/test.gguf", alias="test", **config_overrides)
    instance = SimpleNamespace(config=config, port=8080)
    return LlamaCppRunner().build_command(instance)


def test_speculative_decoding_flags_are_omitted_by_default() -> None:
    command = build_command()

    assert "--spec-type" not in command
    assert "--spec-draft-n-max" not in command


def test_draft_mtp_speculative_decoding_flags_are_added_together() -> None:
    command = build_command(spec_type="draft-mtp", spec_draft_n_max=2)

    spec_type_index = command.index("--spec-type")
    assert command[spec_type_index : spec_type_index + 4] == [
        "--spec-type",
        "draft-mtp",
        "--spec-draft-n-max",
        "2",
    ]


def test_speculative_decoding_flags_are_ignored_for_non_generation_models() -> None:
    command = build_command(
        model_type="embedding", spec_type="draft-mtp", spec_draft_n_max=2
    )

    assert "--spec-type" not in command
    assert "--spec-draft-n-max" not in command


def test_chat_template_kwargs_are_omitted_when_empty() -> None:
    command = build_command(chat_template_kwargs="")

    assert "--chat-template-kwargs" not in command


def test_chat_template_kwargs_normalize_quoted_booleans() -> None:
    command = build_command(chat_template_kwargs='{"enable_thinking": "false"}')

    index = command.index("--chat-template-kwargs")
    assert command[index + 1] == '{"enable_thinking":false}'


def test_chat_template_kwargs_normalize_nested_quoted_booleans() -> None:
    command = build_command(
        chat_template_kwargs='{"outer": {"enable_thinking": "True"}, "list": ["false"]}'
    )

    index = command.index("--chat-template-kwargs")
    assert command[index + 1] == '{"outer":{"enable_thinking":true},"list":[false]}'


def test_chat_template_kwargs_keep_real_booleans_untouched() -> None:
    command = build_command(chat_template_kwargs='{"enable_thinking": true}')

    index = command.index("--chat-template-kwargs")
    assert command[index + 1] == '{"enable_thinking":true}'


def test_chat_template_kwargs_invalid_json_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="not valid JSON"):
        build_command(chat_template_kwargs="{enable_thinking: true}")
