"""The gate: score one pull request against rubric.json.

Inputs: the diff, the repo at the base commit, registries, data sources.
Not inputs: who opened the PR, its title or description, linked issues.

Every rule runs on every PR. Each can only lower the outcome
(reject < review < approve); the final outcome is the lowest one produced.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime, timezone
from pathlib import PurePosixPath

from . import judge as judge_mod
from .checks.common import (HARNESS_DIR, CheckResult, PatchError, PRContext, Unknown,
                            load_json, rule_digests, sha256_file)

TIERS = ["reject", "review", "approve"]


def _applies(rule: dict, ctx: PRContext) -> bool:
    classes = rule.get("applies_to", ["*"])
    return "*" in classes or bool(set(classes) & ctx.classes_touched)


def _run_static(rule: dict, ctx: PRContext) -> CheckResult:
    module = importlib.import_module("harness.checks." + PurePosixPath(rule["script"]).stem)
    try:
        return module.run(ctx, rule.get("params", {}))
    except Unknown as exc:
        return CheckResult(None, str(exc))
    except Exception as exc:  # a crashing check must not look like a passing one
        return CheckResult(None, f"check crashed: {type(exc).__name__}: {exc}")


def _evaluate_rule(rule: dict, kind: str, ctx: PRContext, judge, judge_cfg: dict) -> dict:
    entry = {"id": rule["id"], "kind": kind, "type": rule["type"]}
    if not _applies(rule, ctx):
        return {**entry, "status": "not_applicable", "counted": False, "effect": None,
                "detail": "no changed file in " + ", ".join(rule["applies_to"])}

    if rule["type"] == "static":
        res, rollouts = _run_static(rule, ctx), None
    else:
        res, rollouts = judge_mod.evaluate(rule, ctx, judge, judge_cfg)

    status = {True: "fired", False: "clear", None: "unknown"}[res.fired]
    # Fail closed: an unknown is treated exactly like a fail, for every rule.
    counted = res.fired is not False
    effect = None
    if counted:
        effect = {"gate": "reject", "trigger": "review"}.get(kind) or f"+{rule['weight']}"
    entry.update({"status": status, "counted": counted, "effect": effect,
                  "detail": res.detail, "evidence": res.evidence})
    if kind == "scored":
        entry["weight"] = rule["weight"]
    if rollouts is not None:
        entry["rollouts"] = rollouts
        entry["prompt_sha256"] = judge_mod.prompt_sha256(rule, judge_cfg)
    return entry


def decide(diff_text: str, judge=None, pr_id: str = "local", repo_root=None, data_dir=None,
           now: datetime | None = None, base_sha: str | None = None) -> dict:
    """Return the decision record for one PR.

    base_sha is the commit the repo is checked out at. It is recorded so the
    decision can be recognised as stale once the target branch moves."""
    rubric = load_json(HARNESS_DIR / "rubric.json")
    kwargs = {k: v for k, v in (("repo_root", repo_root), ("data_dir", data_dir)) if v}
    judge = judge or judge_mod.UnavailableJudge()
    now = now or datetime.now(timezone.utc)

    record = {
        "record_version": "1.0",
        "pr": {"id": pr_id, "base_sha": base_sha},
        "rubric": {"version": rubric["rubric_version"],
                   "sha256": sha256_file(HARNESS_DIR / "rubric.json")},
        "registries": rule_digests(),
        "decided_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    try:
        ctx = PRContext(diff_text, rubric=rubric, **kwargs)
        ctx.validate()
    except PatchError as exc:
        # Nothing can be said about a diff that doesn't apply. Fail closed.
        record.update({"results": [], "score": None, "outcome": "reject",
                       "lowered_by": ["diff_does_not_apply"], "error": str(exc)})
        return _seal(record)

    record["pr"].update({
        "diff_sha256": ctx.diff_sha256,
        "changed_paths": [{"path": p, "file_class": ctx.file_class(p)} for p in ctx.changed_paths],
        "services": sorted(ctx.services_touched),
    })

    results = []
    for kind, rules in (("gate", rubric["gates"]),
                        ("trigger", rubric["forced_review"]["triggers"]),
                        ("scored", rubric["scoring"]["checks"])):
        for rule in rules:
            results.append(_evaluate_rule(rule, kind, ctx, judge, rubric["judge"]))

    score = sum(r["weight"] for r in results if r["kind"] == "scored" and r["counted"])
    threshold = rubric["scoring"]["review_threshold"]

    outcome, lowered_by = "approve", []
    for r in results:
        if r["kind"] in ("gate", "trigger") and r["counted"]:
            lowered_by.append(r["id"])
            outcome = min(outcome, r["effect"], key=TIERS.index)
    if score >= threshold:
        lowered_by.append(f"score {score} >= {threshold}")
        outcome = min(outcome, "review", key=TIERS.index)

    record.update({"results": results, "score": score, "review_threshold": threshold,
                   "outcome": outcome, "lowered_by": lowered_by})
    return _seal(record)


def _seal(record: dict) -> dict:
    """Integrity digest over the record. A real deployment signs this with the
    gate's identity; signing is stubbed here."""
    body = json.dumps(record, sort_keys=True, separators=(",", ":"))
    record["record_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return record


def verify_seal(record: dict) -> bool:
    body = {k: v for k, v in record.items() if k != "record_sha256"}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return digest == record.get("record_sha256")
