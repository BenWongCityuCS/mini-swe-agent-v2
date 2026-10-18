from minisweagent.agents.compaction import DEFAULT_SUMMARY_TEMPLATE
from minisweagent.context.compact import compact_threshold, should_compact
from minisweagent.context.compact.prompt import build_summary_template


def test_compact_threshold_reserves_output():
    # max(buffer=100, output_reserve=100) = 100
    assert compact_threshold(1000, 100, 100) == 900
    # Large model output is capped at the default reserve.
    assert compact_threshold(1000, 100, 500_000) < 1000


def test_should_compact_below_and_above():
    below = should_compact(token_count=100, context_limit=1000, buffer=100, output_tokens=100)
    assert not below.should_compact
    assert below.reason == "below_threshold"

    above = should_compact(token_count=950, context_limit=1000, buffer=100, output_tokens=100)
    assert above.should_compact
    assert above.reason == "above_threshold"


def test_circuit_breaker_and_not_enough_messages():
    tripped = should_compact(
        token_count=950,
        context_limit=1000,
        buffer=100,
        output_tokens=100,
        consecutive_failures=3,
        max_consecutive_failures=3,
    )
    assert not tripped.should_compact
    assert tripped.reason == "circuit_breaker"

    early = should_compact(
        token_count=950,
        context_limit=1000,
        buffer=100,
        output_tokens=100,
        enough_messages=False,
    )
    assert not early.should_compact
    assert early.reason == "not_enough_messages"


def test_summary_template_covers_zcode_sections():
    assert "Primary Request and Intent" in DEFAULT_SUMMARY_TEMPLATE
    assert "Files and Code Sections" in DEFAULT_SUMMARY_TEMPLATE
    assert "{{ transcript }}" in DEFAULT_SUMMARY_TEMPLATE

    custom = build_summary_template("focus on the failing tests")
    assert "focus on the failing tests" in custom
    assert "Additional Instructions" in custom
