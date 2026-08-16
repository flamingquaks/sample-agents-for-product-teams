"""Unit tests for the Reviewer eval runner: dataset validation (offline/CI
mode), fail-closed judge-verdict parsing, report aggregation + threshold exit,
and the live-mode payload/invoke shape against a stubbed boto3 client. No
network, no AWS."""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_scoring  # noqa: E402
import run_eval  # noqa: E402


def _case(name="c1", **over):
    base = {
        "name": name,
        "input": "@reviewer (some PR scenario)",
        "expected_behavior": "Agent does the right thing.",
        "tags": ["review_full"],
    }
    base.update(over)
    return base


# --- Dataset validation --------------------------------------------------------


def test_shipped_dataset_is_valid_and_has_20_cases():
    cases = run_eval.load_dataset(str(run_eval.DEFAULT_DATASET))
    assert eval_scoring.validate_dataset(cases) == []
    assert len(cases) == 20


def test_validation_catches_missing_fields_and_bad_tags():
    errors = eval_scoring.validate_dataset([
        _case("ok"),
        {"name": "no_expected", "input": "x", "tags": ["t"]},
        _case("blank_input", input="   "),
        _case("bad_tags", tags=[]),
        _case("tag_types", tags=["ok", 3]),
    ])
    joined = "\n".join(errors)
    assert "no_expected" in joined and "expected_behavior" in joined
    assert "blank_input" in joined and "'input'" in joined
    assert "bad_tags" in joined and "tag_types" in joined
    assert not any("(ok)" in e for e in errors)


def test_validation_catches_duplicate_names_and_non_list():
    errors = eval_scoring.validate_dataset([_case("dup"), _case("dup")])
    assert any("duplicate name" in e for e in errors)
    assert eval_scoring.validate_dataset({"not": "a list"})
    assert eval_scoring.validate_dataset([])


def test_offline_mode_exit_codes(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps([_case("a"), _case("b")]))
    assert run_eval.main(["--mode=offline", "--dataset", str(good)]) == 0

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([_case("a"), {"name": "malformed"}]))
    assert run_eval.main(["--mode=offline", "--dataset", str(bad)]) == 1

    assert run_eval.main(["--mode=offline", "--dataset", str(tmp_path / "missing.json")]) == 1


# --- Judge-response parsing (fail-closed) ---------------------------------------


def test_parse_judge_good_json():
    out = eval_scoring.parse_judge_response('{"pass": true, "reason": "did the thing"}')
    assert out == {"pass": True, "reason": "did the thing"}
    out = eval_scoring.parse_judge_response('{"pass": false, "reason": "missed it"}')
    assert out["pass"] is False


def test_parse_judge_fenced_and_prose_wrapped_json():
    fenced = 'Here is my verdict:\n```json\n{"pass": true, "reason": "ok"}\n```\nDone.'
    assert eval_scoring.parse_judge_response(fenced)["pass"] is True
    prose = 'Verdict follows. {"pass": true, "reason": "ok"} That is all.'
    assert eval_scoring.parse_judge_response(prose)["pass"] is True
    stringy = '{"pass": "true", "reason": "ok"}'
    assert eval_scoring.parse_judge_response(stringy)["pass"] is True


def test_parse_judge_garbage_fails_closed():
    for garbage in (
        "",
        None,
        "the agent did great, definitely a pass!",
        '{"reason": "no pass key"}',
        '{"pass": "maybe", "reason": "hedging"}',
        '{"pass": 1, "reason": "truthy int"}',
        "```json\nnot json\n```",
    ):
        out = eval_scoring.parse_judge_response(garbage)
        assert out["pass"] is False, f"should fail closed on {garbage!r}"
        assert out["reason"]


# --- Aggregation + threshold -----------------------------------------------------


def _results(passes, fails):
    rows = [{"name": f"p{i}", "pass": True, "reason": "ok", "tags": ["a"]}
            for i in range(passes)]
    rows += [{"name": f"f{i}", "pass": False, "reason": "no", "tags": ["a", "b"]}
             for i in range(fails)]
    return rows


def test_aggregate_counts_and_tags():
    report = eval_scoring.aggregate(_results(3, 1))
    assert report["total"] == 4 and report["passed"] == 3 and report["failed"] == 1
    assert report["pass_rate"] == 0.75
    assert report["by_tag"] == {"a": {"passed": 3, "total": 4},
                                "b": {"passed": 0, "total": 1}}


def test_threshold_gate():
    assert eval_scoring.meets_threshold(eval_scoring.aggregate(_results(9, 1)), 0.9)
    assert not eval_scoring.meets_threshold(eval_scoring.aggregate(_results(8, 2)), 0.9)
    assert not eval_scoring.meets_threshold(eval_scoring.aggregate([]), 0.0)


def test_render_table_mentions_cases_and_rate():
    results = _results(1, 1)
    text = eval_scoring.render_table(results, eval_scoring.aggregate(results), 0.9)
    assert "p0" in text and "f0" in text
    assert "PASS" in text and "FAIL" in text
    assert "1/2" in text and "NOT MET" in text


# --- Live-mode payload + invoke shape (stubbed boto3) -----------------------------


class StubClient:
    """Captures invoke_agent_runtime kwargs and returns a canned agent result."""

    def __init__(self, result_text="🔎 **[Reviewer]** posted one COMMENT review."):
        self.calls = []
        self.result_text = result_text

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        body = json.dumps({"result": self.result_text}).encode("utf-8")
        return {"response": io.BytesIO(body)}


def test_build_payload_mirrors_router_shape():
    payload = run_eval.build_payload(_case(), "myorg/eval-repo", 7, "aid-123")
    assert set(payload) == {"prompt", "session_id", "source", "source_context",
                            "assignment_id"}
    assert payload["prompt"] == "@reviewer (some PR scenario)"
    assert payload["session_id"] == payload["assignment_id"] == "aid-123"
    assert payload["source"] == "github"
    assert payload["source_context"] == {"repo": "myorg/eval-repo", "pr_number": 7}


def test_build_payload_flags_automation_cases():
    case = _case(input="(automation pull_request.synchronize; head is def)")
    payload = run_eval.build_payload(case, "o/r", 1, "aid")
    assert payload["source_context"]["automation_event"] == "pull_request.synchronize"
    case = _case(input="(automation pull_request.opened on a DRAFT PR)")
    payload = run_eval.build_payload(case, "o/r", 1, "aid")
    assert payload["source_context"]["automation_event"] == "pull_request.opened"


def test_invoke_case_wire_shape_and_result_extraction():
    client = StubClient()
    payload = run_eval.build_payload(_case(), "o/r", 3, "session-xyz")
    text = run_eval.invoke_case(client, "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
                                payload)
    assert "COMMENT review" in text
    (call,) = client.calls
    assert call["agentRuntimeArn"].startswith("arn:aws:bedrock-agentcore:")
    assert call["qualifier"] == "DEFAULT"
    assert call["contentType"] == call["accept"] == "application/json"
    assert call["runtimeSessionId"] == "session-xyz"
    assert json.loads(call["payload"].decode("utf-8")) == payload


def test_extract_result_text_variants():
    body = json.dumps({"result": "hello"}).encode()
    assert run_eval._extract_result_text({"response": io.BytesIO(body)}) == "hello"
    assert run_eval._extract_result_text({"response": b"plain text"}) == "plain text"
    assert run_eval._extract_result_text({"response": [b"chu", b"nks"]}) == "chunks"
    assert run_eval._extract_result_text({}) == ""


def test_run_live_end_to_end_report_and_exit(tmp_path, monkeypatch, capsys):
    dataset = tmp_path / "ds.json"
    dataset.write_text(json.dumps([
        _case("passes", tags=["review_full"]),
        _case("fails", tags=["safety"]),
    ]))
    out = tmp_path / "report.json"
    stub = StubClient()

    monkeypatch.setattr(run_eval, "_make_agentcore_client", lambda *a, **k: stub)
    monkeypatch.setattr(run_eval, "_build_judge_model", lambda: object())
    verdicts = {"passes": {"pass": True, "reason": "did it"},
                "fails": {"pass": False, "reason": "did not"}}
    current = {}

    def fake_judge(model, expected, output):
        return verdicts[current["name"]]

    monkeypatch.setattr(run_eval, "_judge_output", fake_judge)
    orig_build = run_eval.build_payload

    def tracking_build(case, repo, pr, assignment_id):
        current["name"] = case["name"]
        return orig_build(case, repo, pr, assignment_id)

    monkeypatch.setattr(run_eval, "build_payload", tracking_build)

    # 1/2 passing < 0.9 threshold → exit 1; report still written.
    code = run_eval.main(["--dataset", str(dataset), "--out", str(out),
                          "--runtime-arn", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r",
                          "--repo", "myorg/eval-repo", "--pr", "9"])
    assert code == 1
    report = json.loads(out.read_text())
    assert report["total"] == 2 and report["passed"] == 1
    assert report["threshold"] == 0.9
    assert {c["name"]: c["pass"] for c in report["cases"]} == {
        "passes": True, "fails": False}
    assert all(c["repo"] == "myorg/eval-repo" and c["pr"] == 9
               for c in report["cases"])
    assert len(stub.calls) == 2
    table = capsys.readouterr().out
    assert "passes" in table and "did not" in table

    # Same run with a threshold the results meet → exit 0.
    code = run_eval.main(["--dataset", str(dataset), "--out", str(out),
                          "--runtime-arn", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r",
                          "--repo", "myorg/eval-repo", "--pr", "9",
                          "--threshold", "0.5"])
    assert code == 0


def test_run_live_refuses_unmapped_cases(tmp_path, monkeypatch):
    dataset = tmp_path / "ds.json"
    dataset.write_text(json.dumps([_case("mapped"), _case("unmapped")]))
    mapping = tmp_path / "map.json"
    mapping.write_text(json.dumps({"mapped": {"repo": "o/r", "pr": 1}}))
    monkeypatch.setattr(run_eval, "_make_agentcore_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no invoke")))
    code = run_eval.main(["--dataset", str(dataset), "--map", str(mapping),
                          "--runtime-arn", "arn:aws:x", "--out",
                          str(tmp_path / "r.json")])
    assert code == 2


def test_run_live_only_filter_rejects_unknown_names(tmp_path):
    dataset = tmp_path / "ds.json"
    dataset.write_text(json.dumps([_case("a")]))
    code = run_eval.main(["--dataset", str(dataset), "--only", "nope",
                          "--runtime-arn", "arn:aws:x"])
    assert code == 2
