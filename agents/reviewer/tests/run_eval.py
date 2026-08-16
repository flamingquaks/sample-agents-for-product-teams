"""Reviewer eval runner — executes the 20-case set in eval_dataset.json.

This is the harness behind the roadmap's "live single-repo eval pass on the
20-PR set" gate (docs/roadmap.md, reviewer spec Rollout step 1). Two modes:

  offline (CI): validate the dataset shape only — every case has
      name/input/expected_behavior/tags, names are unique. No AWS calls.
      Non-zero exit on any malformed case.

        python tests/run_eval.py --mode=offline

  live (default): for each case, invoke the DEPLOYED reviewer AgentCore
      runtime with the exact payload shape the Dispatch Router builds
      (infra/dispatch/router.py::invoke_agent — prompt / session_id / source /
      source_context / assignment_id), capture the result text, then score it
      against the case's expected_behavior with an LLM judge on the fleet's
      own model plumbing (agents/shared/bedrock.build_model — Bedrock Mantle;
      never sends temperature). Prints a per-case table + tag aggregates,
      writes a JSON report, exits non-zero when the pass rate is under
      --threshold (default 0.9).

        python tests/run_eval.py --runtime-arn arn:aws:bedrock-agentcore:... \\
            --repo myorg/eval-repo --pr 7 --out eval_report.json

THE MAPPING IS EXPLICIT. The dataset cases are behavioral descriptions, not
fixture repos — the harness does NOT conjure PRs. You point each case at a real
PR that exhibits its scenario, either fleet-wide (--repo owner/name --pr N) or
per case via --map mapping.json:

    {"review_full_finds_real_bug": {"repo": "myorg/eval-repo", "pr": 12},
     "review_incremental_only_new_commits": {"repo": "myorg/eval-repo", "pr": 13}}

Cases without a mapping abort the run before any invoke (use --only to run a
subset). The case's `input` text rides along as the dispatched instruction, and
`(automation pull_request.*)` cases get `automation_event` in source_context so
the agent takes its automation path.

Runtime ARN: pass --runtime-arn, or let the harness resolve `reviewer` from the
SSM registry the router reads (--registry-param, default $REGISTRY_PARAM or
/dispatch/agents — the deployed stack writes /sdlc-agents/<stage>/registry).

Env for live mode — the judge runs through build_model(), so it needs the SAME
env an agent runtime gets (see agents/shared/bedrock.py):
  BEDROCK_GUARDRAIL_ID (+ BEDROCK_GUARDRAIL_VERSION) — required, fail-closed
      (or SDLC_ALLOW_MISSING_GUARDRAIL=1 for a local run)
  AWS_REGION / MANTLE_ENDPOINT / MANTLE_PROJECT_ID / BEDROCK_MODEL_ID — optional
  AWS credentials able to mint a Bedrock bearer token, call
      bedrock-agentcore:InvokeAgentRuntime, and (if resolving the ARN)
      ssm:GetParameter on the registry param.
"""

import argparse
import datetime
import json
import os
import sys
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))               # eval_scoring, however we're imported
sys.path.insert(0, str(_HERE.parents[1]))    # agents/ → shared.bedrock for the judge

import eval_scoring  # noqa: E402

DEFAULT_DATASET = _HERE / "eval_dataset.json"
DEFAULT_REGISTRY_PARAM = "/dispatch/agents"  # router's load_registry default
AGENT_ID = "reviewer"

# Automation-triggered cases carry the event in source_context (agent.py reads
# source_context["automation_event"] to pick the automation trigger line).
_AUTOMATION_EVENTS = ("pull_request.opened", "pull_request.synchronize")


# --- Dataset + mapping -------------------------------------------------------


def load_dataset(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_mapping(path: str | None) -> dict:
    """--map file: {case_name: {"repo": "owner/name", "pr": N}}."""
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        mapping = json.load(f)
    if not isinstance(mapping, dict):
        raise ValueError("--map must be a JSON object keyed by case name")
    return mapping


def resolve_case_target(case: dict, mapping: dict, default_repo, default_pr):
    """(repo, pr) for a case — per-case mapping wins over the global default.
    Either half may come back None; the caller aborts on unmapped cases."""
    entry = mapping.get(case["name"]) or {}
    repo = entry.get("repo") or default_repo
    pr = entry.get("pr", default_pr)
    return repo, pr


# --- Live invocation (mirrors infra/dispatch/router.py) -----------------------


def detect_automation_event(case_input: str) -> str | None:
    """Cases written as "(automation pull_request.X; ...)" run the agent's
    automation path, not the mention path."""
    for event in _AUTOMATION_EVENTS:
        if event in case_input:
            return event
    return None


def build_payload(case: dict, repo: str, pr: int, assignment_id: str) -> dict:
    """EXACTLY the payload shape router.invoke_agent sends to the runtime."""
    source_context = {"repo": repo, "pr_number": pr}
    event = detect_automation_event(case["input"])
    if event:
        source_context["automation_event"] = event
    return {
        "prompt": case["input"],
        "session_id": assignment_id,
        "source": "github",
        "source_context": source_context,
        "assignment_id": assignment_id,
    }


def _extract_result_text(response: dict) -> str:
    """Pull the agent's result text out of an InvokeAgentRuntime response —
    a StreamingBody (application/json) or an iterable of chunks."""
    body = response.get("response")
    if hasattr(body, "read"):
        raw = body.read()
    elif isinstance(body, (bytes, str)):
        raw = body
    elif body is not None:
        raw = b"".join(
            chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
            for chunk in body
        )
    else:
        raw = b""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    # The agent entrypoint returns {"result": "..."} — unwrap when present.
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if isinstance(parsed, dict) and "result" in parsed:
        return str(parsed["result"])
    return text


def invoke_case(client, runtime_arn: str, payload: dict) -> str:
    """One InvokeAgentRuntime call, same kwargs as router._invoke_runtime.
    The payload's assignment_id doubles as the runtime session id (a fresh
    36-char UUID per case — no session reuse between cases)."""
    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        qualifier="DEFAULT",
        contentType="application/json",
        accept="application/json",
        payload=json.dumps(payload).encode("utf-8"),
        runtimeSessionId=payload["session_id"],
    )
    return _extract_result_text(response)


def _make_agentcore_client(region: str | None, read_timeout: int):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "bedrock-agentcore",
        region_name=region or os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(read_timeout=read_timeout, retries={"max_attempts": 0}),
    )


def resolve_runtime_arn(args) -> str:
    """--runtime-arn, or the reviewer entry from the SSM registry the router
    reads (same param + YAML shape as router.load_registry)."""
    if args.runtime_arn:
        return args.runtime_arn
    import boto3
    import yaml

    ssm = boto3.client("ssm", region_name=args.region or os.environ.get("AWS_REGION", "us-east-1"))
    param_name = args.registry_param or os.environ.get("REGISTRY_PARAM", DEFAULT_REGISTRY_PARAM)
    param = ssm.get_parameter(Name=param_name, WithDecryption=False)
    registry = yaml.safe_load(param["Parameter"]["Value"]) or {}
    agent = (registry.get("agents") or {}).get(AGENT_ID) or {}
    arn = agent.get("runtime_arn", "")
    if "${" in arn or not arn.startswith("arn:"):
        raise ValueError(
            f"registry param {param_name} has no live runtime_arn for '{AGENT_ID}' "
            f"({arn!r}) — pass --runtime-arn or onboard the agent first"
        )
    return arn


# --- LLM judge (fleet model plumbing) -----------------------------------------


def _build_judge_model():
    from shared.bedrock import build_model

    return build_model()


def _judge_output(model, expected_behavior: str, actual_output: str) -> dict:
    """One judge call → parsed verdict. A fresh tool-less Agent per case so
    verdicts never leak context between cases; parsing is fail-closed."""
    from strands import Agent

    judge = Agent(
        model=model,
        system_prompt=eval_scoring.JUDGE_SYSTEM_PROMPT,
        tools=[],
        callback_handler=None,
    )
    raw = str(judge(eval_scoring.build_judge_prompt(expected_behavior, actual_output)))
    return eval_scoring.parse_judge_response(raw)


# --- Modes ---------------------------------------------------------------------


def run_offline(dataset_path: str) -> int:
    """CI mode: dataset shape only, no AWS."""
    try:
        cases = load_dataset(dataset_path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: cannot load dataset {dataset_path}: {exc}")
        return 1
    errors = eval_scoring.validate_dataset(cases)
    if errors:
        print(f"FAIL: {len(errors)} dataset problem(s) in {dataset_path}:")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(f"OK: {len(cases)} cases valid in {dataset_path}")
    return 0


def run_live(args) -> int:
    cases = load_dataset(args.dataset)
    errors = eval_scoring.validate_dataset(cases)
    if errors:
        print("FAIL: dataset invalid — run --mode=offline for details")
        return 1

    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        unknown = wanted - {c["name"] for c in cases}
        if unknown:
            print(f"FAIL: --only names not in dataset: {sorted(unknown)}")
            return 2
        cases = [c for c in cases if c["name"] in wanted]

    mapping = load_mapping(args.map)
    targets, unmapped = {}, []
    for case in cases:
        repo, pr = resolve_case_target(case, mapping, args.repo, args.pr)
        if repo and pr is not None:
            targets[case["name"]] = (repo, int(pr))
        else:
            unmapped.append(case["name"])
    if unmapped:
        print(
            "FAIL: no target PR for case(s) — the dataset is behavioral, not "
            "self-executing; supply --repo/--pr or a --map entry (or --only):"
        )
        for name in unmapped:
            print(f"  - {name}")
        return 2

    runtime_arn = resolve_runtime_arn(args)
    client = _make_agentcore_client(args.region, args.invoke_timeout)
    judge_model = _build_judge_model()

    results = []
    for case in cases:
        repo, pr = targets[case["name"]]
        assignment_id = str(uuid.uuid4())
        print(f"[{case['name']}] invoking against {repo}#{pr} ...", flush=True)
        try:
            output = invoke_case(client, runtime_arn, build_payload(case, repo, pr, assignment_id))
        except Exception as exc:  # a dead runtime fails the case, not the run
            results.append({
                "name": case["name"], "tags": case["tags"], "repo": repo, "pr": pr,
                "pass": False, "reason": f"runtime invocation failed: {exc}",
                "assignment_id": assignment_id,
            })
            continue
        try:
            verdict = _judge_output(judge_model, case["expected_behavior"], output)
        except Exception as exc:  # fail-closed: a broken judge never passes
            verdict = {"pass": False, "reason": f"judge call failed: {exc}"}
        results.append({
            "name": case["name"], "tags": case["tags"], "repo": repo, "pr": pr,
            "pass": verdict["pass"], "reason": verdict["reason"],
            "assignment_id": assignment_id,
            "output_excerpt": output[:2000],
        })

    report_aggregates = eval_scoring.aggregate(results)
    print()
    print(eval_scoring.render_table(results, report_aggregates, args.threshold))

    report = {
        "dataset": str(args.dataset),
        "mode": "live",
        "runtime_arn": runtime_arn,
        "threshold": args.threshold,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        **report_aggregates,
        "cases": results,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\nreport written to {args.out}")

    return 0 if eval_scoring.meets_threshold(report_aggregates, args.threshold) else 1


# --- CLI -----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_eval",
        description="Reviewer agent eval runner (see module docstring)",
    )
    parser.add_argument("--mode", choices=("live", "offline"), default="live")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--runtime-arn", default=None,
                        help="reviewer AgentCore runtime ARN (else resolved from SSM)")
    parser.add_argument("--registry-param", default=None,
                        help=f"SSM registry param (default $REGISTRY_PARAM or {DEFAULT_REGISTRY_PARAM})")
    parser.add_argument("--region", default=None)
    parser.add_argument("--repo", default=None, help="default target owner/name for every case")
    parser.add_argument("--pr", type=int, default=None, help="default target PR number")
    parser.add_argument("--map", default=None,
                        help="JSON file: {case_name: {repo, pr}} per-case targets")
    parser.add_argument("--only", default=None, help="comma-separated case names to run")
    parser.add_argument("--out", default="eval_report.json")
    parser.add_argument("--threshold", type=float, default=0.9,
                        help="minimum pass rate for exit 0 (default 0.9)")
    parser.add_argument("--invoke-timeout", type=int, default=900,
                        help="per-case InvokeAgentRuntime read timeout, seconds")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "offline":
        return run_offline(args.dataset)
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
