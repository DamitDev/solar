"""Tests for the minimal Prometheus exposition parser and the per-backend
snapshot mappings (llama.cpp / SGLang metric names → InstanceUsageSnapshot)."""

from solar_host.backends.llamacpp import LlamaCppRunner
from solar_host.backends.prom import parse_prometheus
from solar_host.backends.sglang import SglangRunner


class TestParsePrometheus:
    def test_names_with_colon_namespaces_are_parsed(self):
        values = parse_prometheus(
            "llamacpp:prompt_tokens_total 36886\n" "llamacpp:requests_processing 2\n"
        )

        assert values == {
            "llamacpp:prompt_tokens_total": 36886.0,
            "llamacpp:requests_processing": 2.0,
        }

    def test_help_and_type_comments_are_ignored(self):
        values = parse_prometheus(
            "# HELP sglang:prompt_tokens_total Number of prompt tokens\n"
            "# TYPE sglang:prompt_tokens_total counter\n"
            "sglang:prompt_tokens_total 40960\n"
        )

        assert values == {"sglang:prompt_tokens_total": 40960.0}

    def test_label_sets_are_stripped(self):
        values = parse_prometheus(
            'sglang:token_usage{model="deepseek-v4-flash-284b"} 0.03\n'
        )

        assert values == {"sglang:token_usage": 0.03}

    def test_non_numeric_values_are_skipped(self):
        values = parse_prometheus("some_gauge not-a-number\n" "some_counter 12\n")

        assert values == {"some_counter": 12.0}

    def test_empty_and_comment_only_bodies_parse_to_empty(self):
        assert parse_prometheus("") == {}
        assert parse_prometheus("# only a comment\n") == {}


class TestLlamaCppMetrics:
    def test_expected_counter_names_map_onto_the_snapshot(self):
        snapshot = LlamaCppRunner().parse_metrics(
            "llamacpp:prompt_tokens_total 36886\n"
            "llamacpp:tokens_predicted_total 1382\n"
            "llamacpp:requests_processing 1\n"
            "llamacpp:requests_deferred 2\n"
            "llamacpp:kv_cache_usage_ratio 0.42\n"
        )

        assert snapshot is not None
        assert snapshot.prompt_tokens_total == 36886
        assert snapshot.generated_tokens_total == 1382
        assert snapshot.requests_processing == 1
        assert snapshot.requests_deferred == 2
        assert snapshot.kv_cache_usage_ratio == 0.42

    def test_a_generic_metrics_body_yields_no_snapshot(self):
        snapshot = LlamaCppRunner().parse_metrics(
            "# HELP process_cpu_seconds_total Total user and system CPU time\n"
            "process_cpu_seconds_total 123.4\n"
        )

        assert snapshot is None


class TestSglangMetrics:
    def test_expected_metric_names_map_onto_the_snapshot(self):
        snapshot = SglangRunner().parse_metrics(
            "sglang:prompt_tokens_total 83360\n"
            "sglang:generation_tokens_total 2900\n"
            "sglang:cached_tokens_total 50944\n"
            "sglang:num_running_reqs 3\n"
            "sglang:num_queue_reqs 1\n"
            "sglang:token_usage 0.03\n"
        )

        assert snapshot is not None
        assert snapshot.prompt_tokens_total == 83360
        assert snapshot.generated_tokens_total == 2900
        assert snapshot.cached_tokens_total == 50944
        assert snapshot.requests_processing == 3
        assert snapshot.requests_deferred == 1
        assert snapshot.kv_cache_usage_ratio == 0.03

    def test_gauge_only_body_still_yields_a_snapshot(self):
        snapshot = SglangRunner().parse_metrics("sglang:num_running_reqs 0\n")

        assert snapshot is not None
        assert snapshot.requests_processing == 0
        assert snapshot.prompt_tokens_total is None
