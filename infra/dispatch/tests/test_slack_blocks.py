"""Unit tests for the pure Block Kit formatters in slack_blocks.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import slack_blocks  # noqa: E402


def _flatten_text(blocks):
    """Collect every text string in a Block Kit list for substring assertions."""
    texts = []
    for block in blocks:
        text = block.get("text")
        if isinstance(text, dict):
            texts.append(text.get("text", ""))
        for el in block.get("elements", []) or []:
            if isinstance(el, dict):
                texts.append(el.get("text", ""))
    return "\n".join(texts)


def test_format_agent_ack_contains_agent_id_and_truncated_assignment():
    assignment_id = "abcdef1234567890"
    blocks = slack_blocks.format_agent_ack("workitems", assignment_id)
    assert isinstance(blocks, list)
    assert all(isinstance(b, dict) for b in blocks)
    text = _flatten_text(blocks)
    assert "workitems" in text
    assert assignment_id[:8] in text
    # full id should not appear (it is truncated)
    assert assignment_id not in text


def test_format_agent_result_header_and_conditional_sections():
    # Header uses agent_id.title()
    blocks = slack_blocks.format_agent_result("workitems", "done", assignment_id="abcdef1234", details="extra")
    headers = [b for b in blocks if b["type"] == "header"]
    assert headers
    assert "Workitems" in headers[0]["text"]["text"]

    text = _flatten_text(blocks)
    assert "extra" in text  # details section present
    assert "abcdef12" in text  # assignment context present

    # No details -> only the summary section, no extra section
    no_details = slack_blocks.format_agent_result("workitems", "done")
    section_count = sum(1 for b in no_details if b["type"] == "section")
    assert section_count == 1

    # Both empty -> no context block
    minimal = slack_blocks.format_agent_result("workitems", "done")
    assert not any(b["type"] == "context" for b in minimal)


def test_format_error_includes_message_and_conditional_context():
    blocks = slack_blocks.format_error("something failed", assignment_id="abcdef1234")
    text = _flatten_text(blocks)
    assert "something failed" in text
    assert "abcdef12" in text
    assert any(b["type"] == "context" for b in blocks)

    no_assignment = slack_blocks.format_error("something failed")
    assert not any(b["type"] == "context" for b in no_assignment)


def test_format_guardrail_block_includes_message_and_full_assignment():
    assignment_id = "abcdef1234567890"
    blocks = slack_blocks.format_guardrail_block("not allowed", assignment_id)
    text = _flatten_text(blocks)
    assert "not allowed" in text
    # full id appears, not truncated
    assert assignment_id in text


def test_format_fleet_status_lines_indicators_and_completions():
    counts = {"workitems": 0, "researcher": 3}
    blocks = slack_blocks.format_fleet_status(counts, recent_completions=7)
    text = _flatten_text(blocks)
    # one line per agent
    assert "workitems" in text
    assert "researcher" in text
    # green when 0, blue when > 0
    assert ":green_circle:" in text
    assert ":large_blue_circle:" in text
    # recent_completions number present
    assert "7" in text


def test_all_formatters_return_block_kit_shaped_dicts():
    samples = [
        slack_blocks.format_agent_ack("workitems", "abcdef1234"),
        slack_blocks.format_agent_result("workitems", "done", assignment_id="abcdef1234", details="x"),
        slack_blocks.format_error("oops", assignment_id="abcdef1234"),
        slack_blocks.format_guardrail_block("blocked", "abcdef1234"),
        slack_blocks.format_fleet_status({"workitems": 1}, recent_completions=2),
    ]
    for blocks in samples:
        assert isinstance(blocks, list)
        assert blocks
        for item in blocks:
            assert isinstance(item, dict)
            assert "type" in item
