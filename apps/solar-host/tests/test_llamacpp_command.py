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


def test_multi_gpu_flags_are_omitted_by_default() -> None:
    command = build_command()

    assert "--device" not in command
    assert "--split-mode" not in command
    assert "--tensor-split" not in command
    assert "--main-gpu" not in command


def test_multi_gpu_flags_are_passed_through() -> None:
    command = build_command(
        devices="CUDA0,CUDA1",
        split_mode="row",
        tensor_split="3,1",
        main_gpu=1,
    )

    for flag, expected in (
        ("--device", "CUDA0,CUDA1"),
        ("--split-mode", "row"),
        ("--tensor-split", "3,1"),
        ("--main-gpu", "1"),
    ):
        assert command[command.index(flag) + 1] == expected


def test_main_gpu_zero_is_still_passed() -> None:
    """0 is the meaningful "first GPU" value, not an absent one."""
    command = build_command(main_gpu=0)

    assert command[command.index("--main-gpu") + 1] == "0"


def test_multi_gpu_flags_apply_to_embedding_servers() -> None:
    command = build_command(model_type="embedding", devices="CUDA1")

    assert command[command.index("--device") + 1] == "CUDA1"


def test_device_lists_are_normalized() -> None:
    command = build_command(devices=" CUDA0 , CUDA1 ", tensor_split="3, 1")

    assert command[command.index("--device") + 1] == "CUDA0,CUDA1"
    assert command[command.index("--tensor-split") + 1] == "3,1"


def test_blank_device_lists_are_dropped() -> None:
    command = build_command(devices="  ", tensor_split=" , ")

    assert "--device" not in command
    assert "--tensor-split" not in command


def test_non_numeric_tensor_split_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="comma-separated numbers"):
        build_command(tensor_split="3,half")


def test_negative_tensor_split_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="must not be negative"):
        build_command(tensor_split="3,-1")


def test_non_finite_tensor_split_raises() -> None:
    """float() takes 'inf' and 'nan'; llama.cpp then divides by a nonsense sum."""
    import pytest

    for value in ("3,inf", "3,nan"):
        with pytest.raises(ValueError, match="comma-separated numbers"):
            build_command(tensor_split=value)


# C1 cross-service pin. Duplicated verbatim in
# apps/solar-control/tests/test_reconciliation.py — keep the two in step.
# Control compares an intent's stored backend against what this host writes
# back, so the two coercions agreeing is what makes a canonicalized value read
# as "no drift" instead of trapping the intent in a REPLACE-stop loop. Control's
# test env cannot import solar_host, so identical tables either side is the pin.
COERCION_PARITY_TABLE: list[tuple[object, object]] = [
    ({"a": "true", "b": ["false", "x"]}, {"a": True, "b": [False, "x"]}),
    ({"outer": {"enable_thinking": "True"}}, {"outer": {"enable_thinking": True}}),
    (" TRUE ", True),
    ("False", False),
    ("true", True),
    ("trueish", "trueish"),
    ("", ""),
    (["false"], [False]),
    ({}, {}),
    ([], []),
    (1, 1),
    (0, 0),
    (1.5, 1.5),
    (True, True),
    (None, None),
]


def test_coerce_template_kwargs_matches_controls_copy() -> None:
    """The host half of the parity pin; see COERCION_PARITY_TABLE above."""
    from solar_host.models.llamacpp import _coerce_template_kwargs

    for value, expected in COERCION_PARITY_TABLE:
        result = _coerce_template_kwargs(value)
        # Type too, not just equality: True == 1 in Python, so an
        # equality-only assertion cannot tell a coerced bool from the int it
        # must not become.
        assert result == expected and type(result) is type(
            expected
        ), f"_coerce_template_kwargs({value!r}) == {result!r}, expected {expected!r}"


# Same cross-service pin for the multi-GPU lists. Duplicated verbatim in
# apps/solar-control/tests/test_reconciliation.py — keep the two in step.
CSV_NORMALIZATION_PARITY_TABLE: list[tuple[object, object]] = [
    ("CUDA0,CUDA1", "CUDA0,CUDA1"),
    (" CUDA0 , CUDA1 ", "CUDA0,CUDA1"),
    ("3, 1", "3,1"),
    ("CUDA0,,CUDA1", "CUDA0,CUDA1"),
    ("  ", None),
    (",", None),
    ("", None),
    (None, None),
    (1, 1),
]


def test_normalize_csv_matches_controls_copy() -> None:
    """The host half of the parity pin; see CSV_NORMALIZATION_PARITY_TABLE."""
    from solar_host.models.llamacpp import _normalize_csv

    for value, expected in CSV_NORMALIZATION_PARITY_TABLE:
        result = _normalize_csv(value)
        assert (
            result == expected
        ), f"_normalize_csv({value!r}) == {result!r}, expected {expected!r}"
