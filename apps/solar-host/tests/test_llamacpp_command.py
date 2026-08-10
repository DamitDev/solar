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


def test_draft_dspark_passes_the_draft_model_and_block_size() -> None:
    command = build_command(
        spec_type="draft-dspark",
        spec_draft_model="/models/draft.gguf",
        spec_draft_n_max=7,
    )

    spec_type_index = command.index("--spec-type")
    assert command[spec_type_index : spec_type_index + 6] == [
        "--spec-type",
        "draft-dspark",
        "--spec-draft-model",
        "/models/draft.gguf",
        "--spec-draft-n-max",
        "7",
    ]


def test_draft_dspark_omits_the_optional_flags_when_unset() -> None:
    command = build_command(
        spec_type="draft-dspark", spec_draft_model="/models/draft.gguf"
    )

    assert "--spec-draft-n-max" not in command
    assert "--spec-draft-conf-min" not in command


def test_draft_dspark_confidence_threshold_is_passed_through() -> None:
    command = build_command(
        spec_type="draft-dspark",
        spec_draft_model="/models/draft.gguf",
        spec_draft_conf_min=0.5,
    )

    index = command.index("--spec-draft-conf-min")
    assert command[index + 1] == "0.5"


def test_draft_dspark_requires_a_draft_model() -> None:
    import pytest

    with pytest.raises(ValueError, match="requires 'spec_draft_model'"):
        build_command(spec_type="draft-dspark")


def test_draft_model_is_rejected_without_dspark() -> None:
    import pytest

    with pytest.raises(ValueError, match="only supported with spec_type"):
        build_command(
            spec_type="draft-mtp",
            spec_draft_n_max=2,
            spec_draft_model="/models/draft.gguf",
        )


def test_speculative_decoding_flags_are_ignored_for_non_generation_models() -> None:
    command = build_command(
        model_type="embedding", spec_type="draft-mtp", spec_draft_n_max=2
    )

    assert "--spec-type" not in command
    assert "--spec-draft-n-max" not in command


def test_dspark_flags_are_ignored_for_non_generation_models() -> None:
    command = build_command(
        model_type="embedding",
        spec_type="draft-dspark",
        spec_draft_model="/models/draft.gguf",
    )

    assert "--spec-type" not in command
    assert "--spec-draft-model" not in command


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
