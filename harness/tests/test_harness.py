"""Deterministic tests for the gate, the checks, the judge validation, the
proposer, and the attestation rule. Standard library only:

    python -m unittest discover -s harness/tests -t . -v
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from harness import attest, gate, notify, proposer, stale
from harness import judge as judge_mod
from harness.checks import (dependency_relationship, large_change, major_version_jump,
                            owner_resolved, related_test_excluded, secret_scan,
                            sensitive_area, vulnerable_version, weakens_tests)
from harness.checks.common import (DEFAULT_DATA, DEFAULT_REPO, HARNESS_DIR, PatchError,
                                   PRContext, Unknown, glob_to_regex, load_json)
from harness.checks.deps import in_range
from harness.judge import RecordedJudge, UnavailableJudge

NOW = datetime(2026, 11, 16, 14, 0, tzinfo=timezone.utc)
PRS = HARNESS_DIR / "fixtures" / "prs"
RUBRIC = load_json(HARNESS_DIR / "rubric.json")
PARAMS = {r["id"]: r.get("params", {}) for r in
          RUBRIC["gates"] + RUBRIC["forced_review"]["triggers"] + RUBRIC["scoring"]["checks"]}


def pr(name: str) -> dict:
    return proposer.load_pr(PRS / name)


BASE = "7d57275"


def decide(name: str, judge=None, **kwargs) -> dict:
    p = pr(name)
    return gate.decide(p["diff"], judge or RecordedJudge(p["judge"]), pr_id=name, now=NOW,
                       base_sha=BASE, **kwargs)


def counted(decision: dict) -> set:
    return {r["id"].split("_")[0] for r in decision["results"] if r["counted"]}


def ctx_for(name: str) -> PRContext:
    return PRContext(pr(name)["diff"])


def new_file_diff(path: str, lines: list) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


class Scenarios(unittest.TestCase):
    """The table in DESIGN.md section 6, as assertions."""

    def test_pyyaml_bump_goes_to_review(self):
        d = decide("dep-pyyaml-5.4")
        self.assertEqual(d["outcome"], "review")
        self.assertEqual(d["score"], 100)
        self.assertEqual(counted(d), {"S1", "D5", "Q1", "Q2", "Q3", "Q4"})

    def test_snakeyaml_bump_goes_to_review_despite_green_ci(self):
        d = decide("dep-snakeyaml-2.0")
        self.assertEqual(d["outcome"], "review")
        self.assertEqual(d["score"], 160)
        self.assertEqual(counted(d), {"S1", "D1", "D2", "D3", "Q1", "Q2", "Q3", "Q4"})

    def test_commons_collections_patch_is_forced_to_review_by_money_tag(self):
        d = decide("dep-commons-collections-3.2.2")
        self.assertEqual(d["outcome"], "review")
        s1 = next(r for r in d["results"] if r["id"].startswith("S1"))
        self.assertIn("money", s1["evidence"][0]["domains"])

    def test_excluding_a_failing_test_is_rejected(self):
        d = decide("adv-exclude-failing-test")
        self.assertEqual(d["outcome"], "reject")
        self.assertIn("G1_weakens_verification", d["lowered_by"])

    def test_committing_a_token_is_rejected(self):
        d = decide("adv-ci-token-refresh")
        self.assertEqual(d["outcome"], "reject")
        self.assertIn("G2_secret_in_diff", d["lowered_by"])
        self.assertNotIn("rw_live", json.dumps(d))  # the record must not leak the value

    def test_safe_patch_bump_is_approved(self):
        d = decide("control-jinja2-patch")
        self.assertEqual(d["outcome"], "approve")
        self.assertEqual(d["score"], 10)  # D5 only: report-worker has no owner

    def test_doc_wording_fix_is_approved(self):
        d = decide("doc-readme-heading")
        self.assertEqual((d["outcome"], d["score"]), ("approve", 0))

    def test_policy_doc_change_goes_to_review(self):
        d = decide("doc-contributing-token-claim")
        self.assertEqual(d["outcome"], "review")
        self.assertEqual(counted(d), {"S1", "S3"})

    def test_doc_granting_bots_privileges_is_rejected(self):
        d = decide("doc-grants-bot-bypass")
        self.assertEqual(d["outcome"], "reject")
        self.assertIn("G4_doc_grants_automation_privilege", d["lowered_by"])


class GateRules(unittest.TestCase):
    def test_every_rule_runs_even_after_a_reject(self):
        d = decide("adv-exclude-failing-test")
        self.assertEqual(len(d["results"]), len(PARAMS))
        self.assertTrue(any(r["kind"] == "scored" and r["counted"] for r in d["results"]))

    def test_no_judge_fails_closed_to_review_not_approve(self):
        d = decide("control-jinja2-patch", UnavailableJudge())
        self.assertEqual(d["outcome"], "review")
        q = [r for r in d["results"] if r["type"] == "llm_judge" and r["status"] != "not_applicable"]
        self.assertTrue(q and all(r["status"] == "unknown" and r["counted"] for r in q))

    def test_judge_cannot_lower_what_static_checks_raised(self):
        """A judge that says 'no' to everything can't rescue a rejected PR."""
        p = pr("adv-exclude-failing-test")
        rosy = copy.deepcopy(p["judge"])
        for entry in rosy.values():
            for r in entry["rollouts"]:
                r["result"] = "pass"
        d = gate.decide(p["diff"], RecordedJudge(rosy), now=NOW)
        self.assertEqual(d["outcome"], "reject")
        self.assertLessEqual({"G1", "S1", "D1", "D2", "D3"}, counted(d))

    def test_diff_that_does_not_apply_is_rejected(self):
        diff = "--- a/README.md\n+++ b/README.md\n@@ -1,1 +1,1 @@\n-not the real first line\n+x\n"
        d = gate.decide(diff, now=NOW)
        self.assertEqual((d["outcome"], d["lowered_by"]), ("reject", ["diff_does_not_apply"]))

    def test_decision_is_reproducible_and_sealed(self):
        a, b = decide("dep-pyyaml-5.4"), decide("dep-pyyaml-5.4")
        self.assertEqual(a, b)
        self.assertTrue(gate.verify_seal(a))
        a["outcome"] = "approve"
        self.assertFalse(gate.verify_seal(a))

    def test_record_holds_no_author_or_description(self):
        text = json.dumps(decide("adv-ci-token-refresh"))
        for leaked in ("CONTRIBUTING.md says", "Safe to auto-merge", "dependency-update"):
            self.assertNotIn(leaked, text)


class StaleDecisions(unittest.TestCase):
    """Another PR merging first can make an approved PR unsafe without changing its diff."""

    def test_decision_is_fresh_on_the_base_it_was_scored_against(self):
        p = pr("control-jinja2-patch")
        self.assertEqual(stale.reasons(decide("control-jinja2-patch"), BASE, p["diff"]), [])

    def test_decision_goes_stale_when_the_target_branch_moves(self):
        why = stale.reasons(decide("control-jinja2-patch"), "abc1234")
        self.assertEqual(len(why), 1)
        self.assertIn("target branch moved", why[0])

    def test_decision_without_a_recorded_base_is_stale(self):
        p = pr("control-jinja2-patch")
        d = gate.decide(p["diff"], RecordedJudge(p["judge"]), now=NOW)
        self.assertTrue(stale.reasons(d, BASE))

    def test_decision_goes_stale_when_the_diff_changes(self):
        why = stale.reasons(decide("control-jinja2-patch"), BASE, pr("dep-pyyaml-5.4")["diff"])
        self.assertIn("the diff changed since the decision", why)

    def test_same_diff_rescored_on_a_moved_base_loses_its_approval(self):
        """The control PR is approved today. Suppose another PR merges first and
        guts the one assertion that made the bump safe. Same diff, new base."""
        self.assertEqual(decide("control-jinja2-patch")["outcome"], "approve")
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(DEFAULT_REPO / "services", Path(tmp) / "services",
                            ignore=shutil.ignore_patterns("target", "__pycache__", ".pytest_cache"))
            test_file = Path(tmp) / "services/report-worker/tests/test_config.py"
            text = test_file.read_text(encoding="utf-8")
            self.assertIn('assert "SKU-1001: 42" in out', text)
            test_file.write_text(text.replace('assert "SKU-1001: 42" in out', "pass"), encoding="utf-8")
            d = decide("control-jinja2-patch", repo_root=tmp)
        self.assertEqual(d["outcome"], "review")
        q2 = next(r for r in d["results"] if r["id"] == "Q2_changed_behavior_untested")
        self.assertEqual(q2["status"], "unknown")  # its citation no longer exists


class JudgePrompts(unittest.TestCase):
    JUDGE_RULES = [r for r in RUBRIC["gates"] + RUBRIC["forced_review"]["triggers"]
                   + RUBRIC["scoring"]["checks"] if r["type"] == "llm_judge"]

    def test_every_judge_check_has_a_complete_prompt(self):
        self.assertTrue(self.JUDGE_RULES)
        for rule in self.JUDGE_RULES:
            text = (HARNESS_DIR / rule["prompt"]).read_text(encoding="utf-8")
            for section in ("## FAIL if", "## PASS if", "## Examples: FAIL",
                            "## Counterexamples: PASS", "## UNKNOWN if"):
                self.assertIn(section, text, f"{rule['prompt']} is missing '{section}'")

    def test_rendered_prompt_contains_instructions_check_tools_and_diff(self):
        rule = next(r for r in self.JUDGE_RULES if r["id"].startswith("Q1"))
        ctx = ctx_for("dep-snakeyaml-2.0")
        text = judge_mod.render_prompt(rule, ctx, RUBRIC["judge"])
        self.assertIn('"result": "pass" | "fail" | "unknown"', text)  # shared output contract
        self.assertIn("## Counterexamples: PASS", text)                # the check's own prompt
        self.assertIn("`api_diff`", text)                              # a granted tool
        self.assertNotIn("`get_finding`", text)                        # a tool not granted
        self.assertIn("+    <snakeyaml.version>2.0</snakeyaml.version>", text)

    def test_rendered_prompt_never_includes_pr_description(self):
        rule = self.JUDGE_RULES[0]
        p = pr("adv-ci-token-refresh")
        text = judge_mod.render_prompt(rule, PRContext(p["diff"]), RUBRIC["judge"])
        self.assertNotIn(p["description"], text)

    def test_decision_records_which_prompt_was_used(self):
        d = decide("dep-pyyaml-5.4")
        judged = [r for r in d["results"] if r["type"] == "llm_judge" and "rollouts" in r]
        self.assertTrue(judged and all(len(r["prompt_sha256"]) == 64 for r in judged))

    def test_editing_a_prompt_makes_old_decisions_stale(self):
        d = decide("control-jinja2-patch")
        d["registries"]["prompts"] = "0" * 64
        self.assertIn("prompts changed since the decision", stale.reasons(d, BASE))


class JudgeValidation(unittest.TestCase):
    QID = "Q2_changed_behavior_untested"

    def _with(self, mutate) -> dict:
        p = pr("control-jinja2-patch")
        recordings = copy.deepcopy(p["judge"])
        mutate(recordings[self.QID]["rollouts"])
        d = gate.decide(p["diff"], RecordedJudge(recordings), now=NOW)
        return next(r for r in d["results"] if r["id"] == self.QID)

    def test_agreeing_rollouts_with_real_evidence_are_accepted(self):
        self.assertEqual(self._with(lambda r: None)["status"], "clear")

    def test_disagreement_is_unknown(self):
        def flip(rollouts):
            rollouts[1] = {**rollouts[1], "result": "fail"}
        r = self._with(flip)
        self.assertEqual((r["status"], r["counted"]), ("unknown", True))

    def test_hallucinated_citation_is_unknown(self):
        def fake(rollouts):
            rollouts[:] = [{**x, "evidence": [{"file": "services/report-worker/tests/test_config.py",
                                               "lines": "2", "contains": "assert everything_is_fine"}]}
                           for x in rollouts]
        self.assertEqual(self._with(fake)["status"], "unknown")

    def test_answer_without_evidence_is_unknown(self):
        def strip(rollouts):
            rollouts[:] = [{**x, "evidence": []} for x in rollouts]
        self.assertEqual(self._with(strip)["status"], "unknown")

    def test_tool_not_granted_to_the_check_is_unknown(self):
        def sneak(rollouts):
            rollouts[:] = [{**x, "tool_calls": ["fetch_release_notes"]} for x in rollouts]
        self.assertEqual(self._with(sneak)["status"], "unknown")

    def test_too_few_rollouts_is_unknown(self):
        def drop(rollouts):
            del rollouts[1:]
        self.assertEqual(self._with(drop)["status"], "unknown")


class StaticChecks(unittest.TestCase):
    def test_globs(self):
        self.assertTrue(glob_to_regex("**/pom.xml").match("pom.xml"))
        self.assertTrue(glob_to_regex("**/pom.xml").match("services/a/pom.xml"))
        self.assertTrue(glob_to_regex(".github/**").match(".github/workflows/ci.yml"))
        self.assertFalse(glob_to_regex("*.md").match("docs/a.md"))

    def test_version_ranges(self):
        self.assertTrue(in_range("5.3.1", "< 5.4"))
        self.assertFalse(in_range("5.4", "< 5.4"))
        self.assertTrue(in_range("1.33", "<= 1.33"))
        self.assertFalse(in_range("2.0", "<= 1.33"))
        self.assertFalse(in_range("3.2.2", "< 3.2.2"))

    def test_adding_a_test_does_not_trip_weakens_tests(self):
        path = "services/report-worker/tests/test_new.py"
        ctx = PRContext(new_file_diff(path, ["def test_x():", "    assert 1 + 1 == 2"]))
        self.assertFalse(weakens_tests.run(ctx, PARAMS["G1_weakens_verification"]).fired)

    def test_removing_assertions_trips_weakens_tests(self):
        path = "services/report-worker/tests/test_config.py"
        lines = (DEFAULT_REPO / path).read_text(encoding="utf-8").splitlines()
        body = "".join((" " if i < len(lines) - 2 else "-") + line + "\n" for i, line in enumerate(lines))
        diff = f"--- a/{path}\n+++ b/{path}\n@@ -1,{len(lines)} +1,{len(lines) - 2} @@\n{body}"
        self.assertTrue(weakens_tests.run(PRContext(diff), PARAMS["G1_weakens_verification"]).fired)

    def test_skip_marker_trips_weakens_tests(self):
        path = "services/report-worker/tests/test_new.py"
        ctx = PRContext(new_file_diff(path, ["import pytest", "@pytest.mark.skip", "def test_x():", "    assert True"]))
        self.assertTrue(weakens_tests.run(ctx, PARAMS["G1_weakens_verification"]).fired)

    def test_secret_scan_ignores_references_to_a_secret_store(self):
        ok = new_file_diff(".github/workflows/x.yml", [
            "env:", "  REGISTRY_TOKEN: ${{ secrets.REGISTRY_TOKEN }}",
            "  API_KEY: <team vault, item report-worker/ci>"])
        self.assertFalse(secret_scan.run(PRContext(ok), {}).fired)
        bad = new_file_diff("notes.md", ["key: ghp_" + "a1B2" * 9])
        self.assertTrue(secret_scan.run(PRContext(bad), {}).fired)

    def test_bump_that_stays_vulnerable_is_caught(self):
        lock = "services/report-worker/requirements.lock"
        diff = (f"--- a/{lock}\n+++ b/{lock}\n@@ -3,1 +3,1 @@\n-PyYAML==5.3.1\n+PyYAML==5.3.2\n")
        res = vulnerable_version.run(PRContext(diff), PARAMS["G3_ships_known_vulnerable_version"])
        self.assertTrue(res.fired)
        self.assertEqual(res.evidence[0]["cve"], "CVE-2020-14343")

    def test_fixed_version_passes_vulnerable_version(self):
        res = vulnerable_version.run(ctx_for("dep-pyyaml-5.4"), PARAMS["G3_ships_known_vulnerable_version"])
        self.assertFalse(res.fired)

    def test_sensitive_area_follows_dependency_usage(self):
        res = sensitive_area.run(ctx_for("dep-commons-collections-3.2.2"), {})
        self.assertTrue(res.fired)
        self.assertTrue(res.evidence[0]["file"].endswith("PricingService.java"))
        self.assertFalse(sensitive_area.run(ctx_for("control-jinja2-patch"), {}).fired)

    def test_dependency_relationship_and_major_jump(self):
        snake, yaml = ctx_for("dep-snakeyaml-2.0"), ctx_for("dep-pyyaml-5.4")
        d1 = PARAMS["D1_not_direct_dependency"]
        self.assertTrue(dependency_relationship.run(snake, d1).fired)
        self.assertFalse(dependency_relationship.run(yaml, d1).fired)
        self.assertTrue(major_version_jump.run(snake, {}).fired)
        self.assertFalse(major_version_jump.run(yaml, {}).fired)

    def test_related_excluded_test_is_found(self):
        res = related_test_excluded.run(ctx_for("dep-snakeyaml-2.0"), {})
        self.assertTrue(res.fired)
        self.assertTrue(res.evidence[0]["test"].endswith("ConfigLoaderIntegrationTest.java"))
        self.assertFalse(related_test_excluded.run(ctx_for("dep-pyyaml-5.4"), {}).fired)

    def test_large_change(self):
        big = new_file_diff("services/report-worker/report_worker/big.py", ["x = 1"] * 301)
        self.assertTrue(large_change.run(PRContext(big), PARAMS["D4_large_change"]).fired)
        self.assertFalse(large_change.run(ctx_for("dep-pyyaml-5.4"), PARAMS["D4_large_change"]).fired)

    def test_owner_resolved(self):
        self.assertTrue(owner_resolved.run(ctx_for("dep-pyyaml-5.4"), {}).fired)      # report-worker
        self.assertFalse(owner_resolved.run(ctx_for("dep-snakeyaml-2.0"), {}).fired)  # inventory-api
        self.assertFalse(owner_resolved.run(ctx_for("doc-readme-heading"), {}).fired) # repo-wide

    def test_missing_data_source_is_unknown_not_clear(self):
        with tempfile.TemporaryDirectory() as empty:
            ctx = PRContext(pr("dep-pyyaml-5.4")["diff"], data_dir=empty)
            with self.assertRaises(Unknown):
                vulnerable_version.run(ctx, PARAMS["G3_ships_known_vulnerable_version"])
            d = gate.decide(pr("dep-pyyaml-5.4")["diff"], data_dir=empty, now=NOW)
            self.assertEqual(d["outcome"], "reject")  # G3 unknown -> reject


class Proposer(unittest.TestCase):
    FINDINGS = load_json(DEFAULT_REPO / "findings" / "findings.json")

    def test_reverification_overrides_the_scanner(self):
        actions = {a["finding"]["id"]: a for a in proposer.run(self.FINDINGS, now=NOW)}
        self.assertEqual(actions["F-40220"]["action"], "pr")
        self.assertEqual(actions["F-40220"]["target_version"], "3.2.2")
        self.assertTrue(any("3.2.2" in n for n in actions["F-40220"]["reverification"]["notes"]))
        self.assertEqual(actions["F-40219"]["feed_age_days"], 105)

    def test_refuses_when_no_fix_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(DEFAULT_DATA, tmp, dirs_exist_ok=True)
            db = load_json(Path(tmp) / "vulndb.json")
            for adv in db["advisories"]:
                if adv["cve"] == "CVE-2015-6420":
                    adv.update(vulnerable_range="<= 3.2.1", fixed_in=None)
            (Path(tmp) / "vulndb.json").write_text(json.dumps(db), encoding="utf-8")
            actions = {a["finding"]["id"]: a for a in proposer.run(self.FINDINGS, now=NOW, data_dir=tmp)}
        self.assertEqual(actions["F-40220"]["action"], "refuse")
        self.assertNotIn("pr", actions["F-40220"])

    def test_escalates_when_the_database_does_not_confirm_the_finding(self):
        doc = copy.deepcopy(self.FINDINGS)
        doc["findings"] = [{**doc["findings"][1], "cve": "CVE-2099-0001"}]
        self.assertEqual(proposer.run(doc, now=NOW)[0]["action"], "escalate")


class Escalation(unittest.TestCase):
    def test_reject_and_unowned_service_notify_the_fallback_channel(self):
        msgs = notify.escalations(decide("adv-ci-token-refresh"))
        self.assertEqual([m["channel"] for m in msgs], ["#platform-oncall"])
        self.assertLessEqual({"E1_sensitive_area", "E2_gate_reject", "E4_unowned_service"},
                             set(msgs[0]["escalation_rules"]))

    def test_overdue_money_finding_notifies_the_service_channel(self):
        finding = next(f for f in Proposer.FINDINGS["findings"] if f["id"] == "F-40220")
        msgs = notify.escalations(decide("dep-commons-collections-3.2.2"), finding)
        self.assertEqual(msgs[0]["channel"], "#inventory-alerts")
        self.assertIn("E3_critical_or_overdue_finding", msgs[0]["escalation_rules"])

    def test_quiet_approval_sends_nothing(self):
        self.assertEqual(notify.escalations(decide("doc-readme-heading")), [])

    def test_notification_does_not_change_the_decision(self):
        d = decide("adv-ci-token-refresh")
        before = copy.deepcopy(d)
        notify.escalations(d)
        self.assertEqual(d, before)


class Attestation(unittest.TestCase):
    def setUp(self):
        self.finding = next(f for f in Proposer.FINDINGS["findings"] if f["id"] == "F-40219")
        self.decision = decide("dep-pyyaml-5.4")
        self.pipeline = load_json(HARNESS_DIR / "fixtures" / "pipeline" / "F-40219.json")
        self.pipeline["change"]["merged_diff_sha256"] = self.decision["pr"]["diff_sha256"]
        self.pipeline["change"]["base_sha_at_merge"] = BASE

    def build(self):
        return attest.build(self.finding, self.decision, self.pipeline)

    def test_remediation_date_is_the_runtime_check_not_the_merge(self):
        a = self.build()
        self.assertEqual(a["remediated_in_production_at"], "2026-11-17T09:00:00Z")
        self.assertNotEqual(a["remediated_in_production_at"], a["change"]["merged_at"])

    def test_vulnerable_instances_still_running_means_not_remediated(self):
        self.pipeline["runtime_verification"]["instances_with_vulnerable_version"] = 2
        a = self.build()
        self.assertIsNone(a["remediated_in_production_at"])
        self.assertIn("vulnerable instances are still running", a["not_remediated_because"])

    def test_merged_and_deployed_without_a_runtime_check_is_not_remediated(self):
        self.pipeline["runtime_verification"] = None
        self.assertIsNone(self.build()["remediated_in_production_at"])

    def test_review_without_the_required_approval_is_not_remediated(self):
        self.pipeline["approval"] = None
        self.assertIsNone(self.build()["remediated_in_production_at"])

    def test_merging_a_different_diff_than_was_scored_is_not_remediated(self):
        self.pipeline["change"]["merged_diff_sha256"] = "0" * 64
        self.assertIsNone(self.build()["remediated_in_production_at"])

    def test_merging_after_the_target_branch_moved_is_not_remediated(self):
        self.pipeline["change"]["base_sha_at_merge"] = "abc1234"
        a = self.build()
        self.assertIsNone(a["remediated_in_production_at"])
        self.assertTrue(any("stale decision" in r for r in a["not_remediated_because"]))

    def test_tampered_decision_is_not_remediated(self):
        self.decision["outcome"] = "approve"
        self.assertIsNone(self.build()["remediated_in_production_at"])


if __name__ == "__main__":
    unittest.main()
