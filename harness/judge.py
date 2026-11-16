"""LLM-judge checks: build the prompt, then distrust the answer.

Each llm_judge rule in rubric.json points at a prompt file in harness/prompts/.
The model sees, in order: the shared judge instructions (_judge.md), the check's
own prompt (fail/pass criteria, examples, counterexamples), the diff, and the
tools granted to that check. See exactly what it would get with:

    python -m harness.judge --show Q1_relied_on_behavior_changes [--pr <recorded PR dir>]

The judge itself is pluggable (``ask`` returns raw rollouts). A recorded judge
replays fixtures; a live one would send render_prompt(...) to a model. Either
way the same validation applies, and that validation is the part that matters.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import Counter
from pathlib import Path

from .checks.common import HARNESS_DIR, CheckResult, PRContext, is_denied, load_json

RESULTS = {"pass", "fail", "unknown"}


# --------------------------------------------------------------------------- prompts

def _read(rel: str) -> str:
    return (HARNESS_DIR / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def prompt_sha256(check: dict, judge_cfg: dict) -> str:
    """Digest of exactly the instructions this check sends (shared + per-check)."""
    text = _read(judge_cfg["system_prompt"]) + "\n" + _read(check["prompt"])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_prompt(check: dict, ctx: PRContext, judge_cfg: dict) -> str:
    tools = {t["name"]: t for t in load_json(HARNESS_DIR / "tools.json")["tools"]}
    granted = [tools[n] for n in check.get("tools", []) if n in tools]
    tool_lines = "\n".join(f"- `{t['name']}`: {t['description']}" for t in granted) or "- (none)"
    changed = "\n".join(f"- {p} ({ctx.file_class(p)})" for p in ctx.changed_paths) or "- (none)"
    return (
        f"{_read(judge_cfg['system_prompt']).rstrip()}\n\n"
        f"---\n\n{_read(check['prompt']).rstrip()}\n\n"
        f"---\n\n## Tools you may call for this check\n\n{tool_lines}\n\n"
        f"## Changed files\n\n{changed}\n\n"
        f"## The diff\n\n```diff\n{ctx.diff_text.rstrip()}\n```\n\n"
        f"Reply with the JSON object only.\n"
    )


# --------------------------------------------------------------------------- judges

class RecordedJudge:
    """Replays recorded rollouts: {check_id: {"rollouts": [...]}}."""

    def __init__(self, recordings: dict | None = None):
        self.recordings = recordings or {}

    def ask(self, check: dict, ctx: PRContext, prompt: str):
        entry = self.recordings.get(check["id"])
        return entry["rollouts"] if entry else None


class UnavailableJudge:
    """No judge configured. Every judge check becomes unknown and fails closed."""

    def ask(self, check: dict, ctx: PRContext, prompt: str):
        return None


# --------------------------------------------------------------------------- validation

def _verify_evidence(ctx: PRContext, item: dict) -> str | None:
    """Return None if the citation checks out, else the reason it doesn't."""
    path, lines, needle = item.get("file"), item.get("lines"), item.get("contains")
    if not path or not lines or not needle:
        return "citation needs file, lines, and contains"
    if is_denied(path):
        return f"{path}: third-party or build path"
    # Cite "side": "base" to point at a line the diff removes.
    text = ctx.base(path) if item.get("side") == "base" else ctx.head(path)
    if text is None:
        return f"{path}: no such file"
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", str(lines))
    if not m:
        return f"{path}: bad line range '{lines}'"
    start, end = int(m[1]), int(m[2] or m[1])
    file_lines = text.splitlines()
    if not (1 <= start <= end <= len(file_lines)):
        return f"{path}:{lines}: outside the file"
    if needle not in "\n".join(file_lines[start - 1:end]):
        return f"{path}:{lines}: does not contain '{needle}'"
    return None


def evaluate(check: dict, ctx: PRContext, judge, judge_cfg: dict):
    """Return (CheckResult, rollout summary) for one llm_judge rule."""
    allowed_tools = set(check.get("tools", []))
    known_tools = {t["name"] for t in load_json(HARNESS_DIR / "tools.json")["tools"]}
    rollouts = judge.ask(check, ctx, render_prompt(check, ctx, judge_cfg))
    need = judge_cfg["rollouts"]

    if not rollouts or len(rollouts) < need:
        got = len(rollouts or [])
        return CheckResult(None, f"judge unavailable ({got}/{need} rollouts)"), []

    results, problems, evidence = [], [], []
    for i, r in enumerate(rollouts[:need], 1):
        result = r.get("result")
        if result not in RESULTS:
            problems.append(f"rollout {i}: invalid result '{result}'")
            result = "unknown"
        bad_tools = set(r.get("tool_calls", [])) - (allowed_tools & known_tools)
        if bad_tools:
            problems.append(f"rollout {i}: used tools not granted to this check: {sorted(bad_tools)}")
            result = "unknown"
        if result != "unknown" and judge_cfg.get("require_evidence"):
            cites = r.get("evidence", [])
            errors = [e for e in (_verify_evidence(ctx, c) for c in cites) if e]
            if not cites:
                errors = ["no evidence cited"]
            if errors:
                problems.append(f"rollout {i}: " + "; ".join(errors))
                result = "unknown"
            else:
                evidence.extend({**c, "verified": True} for c in cites if c not in evidence)
        results.append(result)

    summary = [{"result": x} for x in results]
    top, count = Counter(results).most_common(1)[0]
    if problems:
        return CheckResult(None, "; ".join(problems)), summary
    if top == "unknown" or count / need < judge_cfg["min_agreement"]:
        return CheckResult(None, f"rollouts did not agree: {results}"), summary
    return CheckResult(top == "fail", f"judge answered '{top}' in {count}/{need} rollouts",
                       evidence), summary


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    from .proposer import PRS_DIR, load_pr

    ap = argparse.ArgumentParser(description="Print the exact prompt a judge check would send.")
    ap.add_argument("--show", required=True, help="check id, e.g. Q1_relied_on_behavior_changes")
    ap.add_argument("--pr", default=str(PRS_DIR / "dep-snakeyaml-2.0"), help="recorded PR directory")
    args = ap.parse_args(argv)

    rubric = load_json(HARNESS_DIR / "rubric.json")
    rules = rubric["gates"] + rubric["forced_review"]["triggers"] + rubric["scoring"]["checks"]
    check = next((r for r in rules if r["id"] == args.show and r["type"] == "llm_judge"), None)
    if check is None:
        ids = [r["id"] for r in rules if r["type"] == "llm_judge"]
        print(f"no llm_judge check '{args.show}'. Choose from: {', '.join(ids)}", file=sys.stderr)
        return 2
    ctx = PRContext(load_pr(Path(args.pr))["diff"], rubric=rubric)
    sys.stdout.write(render_prompt(check, ctx, rubric["judge"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
