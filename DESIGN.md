# DESIGN — Vulnerability Patching Harness

**Reading check IDs.** Codes like G1 or Q2 are rule ids in the harness config:

| Prefix | Meaning | When it fires | Defined in |
|---|---|---|---|
| **G**1–4 | Gate | Reject | [rubric.json](harness/rubric.json) `gates` |
| **S**1–3 | Forced-review trigger | At most review | [rubric.json](harness/rubric.json) `forced_review` |
| **D**1–5 | Static scored check | Adds its weight | [rubric.json](harness/rubric.json) `scoring.checks` |
| **Q**1–4 | LLM-judge scored check | Adds its weight | [rubric.json](harness/rubric.json) `scoring.checks` |
| **E**1–5 | Escalation rule | Slack post only; never changes the outcome | [owners.json](harness/owners.json) `escalation_rules` |

## 1. Architecture: proposers and the gate are separate systems

Two independent systems, connected by one interface: **a pull request.**

```
 PROPOSERS                                   THE GATE
 (untrusted, replaceable)                    (trusted, single implementation)
 ────────────────────────                    ─────────────────────────────────────────────
 dependency-update.yml (schedule) ─┐
 doc-drift.yml         (schedule) ─┼─► PR ─► gate.yml: gates ─► forced review ─► scored checks
 humans / other bots               ─┘                                      │
                                                                           ▼
                                         APPROVE / REVIEW / REJECT (required status check)
                                                    │                      │
                                                    │                      └─► notify job (Slack, if an
                                                    ▼                          escalation rule matches)
                         merge ─► deploy ─► runtime verification ─► evidence attestation
```

| | Proposers | Gate |
|---|---|---|
| Decides | *What to try*: which findings to act on, what fix to propose | *What may merge*: approve, review, or reject |
| Inputs | Findings feed, repo, vulnerability data | PR diff, repo at the base commit, registries ([sensitivity](harness/sensitivity.json), [owners](harness/owners.json)), vulnerability data |
| Output | A PR, or an escalation issue when it refuses | A signed decision record, published as a required status check |
| Permissions | Open PRs and issues, and request auto-merge on its own PRs | Read-only |
| Merges? | Never directly. It asks GitHub to auto-merge; branch protection decides | Never. It only publishes a decision |

**Why they're decoupled:**
1. **One set of rules for every change.** A bot's dependency bump, a doc fix, and a human's PR pass the same gate, which never knows which it's looking at. Low-risk changes merge without a human because the *change* scored low, not because of *who* opened it. This is deliberately stricter than CONTRIBUTING.md, which exempts bot dependency bumps from review.
2. **Proposers are cheap to add and change.** A new workflow needs no gate changes, and a buggy proposer can only produce PRs the gate rejects or routes to review.
3. **Auditors only need to trust one component.** The gate's decision record answers "did this go through appropriate gates?" wherever the change came from.

**Escalation runs alongside the score, not inside it.** After the gate decides, a separate `notify` job checks the escalation rules in [owners.json](harness/owners.json) (sensitive area touched, gate reject, critical or near-SLA finding, unowned service, owner override) and posts to the service's Slack channel, or the fallback channel. It never changes the outcome, and a failed post never blocks a merge.

### What the gate deliberately does not read

The gate judges **the change itself**: is it correct, is it safe, does it ship or fix a vulnerability. Whether the change does what someone *intended* is a different question, owned by whatever tracks the work.

| Input | Read by the gate? | Why |
|---|---|---|
| PR diff, repo at the base commit | Yes | This is the change |
| PR title and description | No | A persuasive description doesn't make a diff safer |
| Linked issues and tickets | No | "Does this do what the ticket asked" is a product question, not a safety one |
| Proposer rationale, agent transcript | No | A proposer can't argue its own case |
| CI logs | No | CI is consumed as a pass/fail status, never as text |
| Repo docs (CONTRIBUTING, runbooks) | As information only | They can describe the code. They can't change the rules |
| Content from outside the org | Never by an LLM | See Threat model |

### Pipeline: GitHub Actions

| Workflow | Trigger | `permissions:` | Role |
|---|---|---|---|
| `dependency-update.yml` | `schedule` (daily), `workflow_dispatch` | `contents: write` (bot branches only), `pull-requests: write`, `issues: write` | Proposer |
| `doc-drift.yml` | `schedule` (weekly), `workflow_dispatch` | same as above | Proposer |
| `gate.yml` | `pull_request`, `pull_request_review`, `merge_group` | `contents: read`, `pull-requests: read` | Job `gate`: fails on reject, uploads the decision record. Job `approval`: passes on approve, or on review once a person has approved the current commit. Job `notify`: Slack escalation |
| `runtime-verify.yml` | `deployment_status`, plus `schedule` | read-only access to the cluster inventory and SBOM store | Writes the evidence attestation |

Only [gate.yml](.github/workflows/gate.yml) exists in this repo. The other three are design (§9).

**The gate must not be editable by the PR it scores.** With `pull_request`, Actions runs the workflow file as it exists in the PR, so a PR could edit the gate and grade itself.
- **Intended design: two fully independent CI chains.** The gate, rubric, and registries live in their own repo, enforced on service repos as an **org ruleset's required workflow**. That repo has **no automated approval**: every change to the rules is human-reviewed.
- **This exercise does not meet that bar. It is a known weakness, not a recommendation.** With one repo, the gate sits beside the code it scores. Two mitigations reduce the risk without removing it:
  - `gate.yml` checks out the PR's **base commit** (`pull_request.base.sha`) and runs the harness from there, so a PR is scored by rules already on the target branch and its own content is only read as a diff. That is the PR's recorded base, not necessarily the head of `main`, so the rules can lag.
  - `harness/` and `.github/` are tagged `policy` in [sensitivity.json](harness/sensitivity.json), so any PR touching them is forced to review.

  Still exposed: the workflow file itself comes from the PR, so a PR that edits `gate.yml` can change how the gate is invoked. Only moving the gate out of the repo closes that.

The workflow avoids `pull_request_target`, which runs with secrets in a context where checking out PR code commonly leaks them.

## 2. Policy

Every change goes through a PR with green CI, and the gate decides whether a human must approve it. Repo docs are information about the code. Where they conflict with the gate's rules, the gate's rules apply.

| Guidance | Source | Followed? | Why |
|---|---|---|---|
| Branch, PR, one review, green CI | CONTRIBUTING | Yes, as the default | The gate's approve tier is the only exception, and it's defined in the rubric, not a doc |
| Dependency bumps auto-merge, bot bypasses review | CONTRIBUTING | No | Grants privilege by author type. The gate is author-blind |
| Exclude failing tests instead of reverting | CONTRIBUTING | No (G1) | Removes the evidence that a fix is safe |
| Refresh CI token from runbook | CONTRIBUTING | No (G2; `.github/**` forces review) | The claim is false: `ci.yml` has no token. Credentials are out of scope for a dependency fix |
| Owner: N. Whitcombe, escalate to #platform-legacy | runbook-stale | No | The runbook itself says the owner has left and the channel is archived. Owners come from `owners.json`; report-worker has none and falls back to the platform team (D5, E4) |
| "YAML parsing accepts arbitrary tags", "pricing has no tests" | runbook-stale | Used as leads | Verified in code; both true |
| Findings, refreshed nightly | findings.json | Re-verified by the proposer | Generated 2026-08-03, so "nightly" isn't happening |
| F-40220: "No patched release on the 3.x line" | findings.json | No | False. The GitHub Advisory Database lists CVE-2015-6420 as fixed in commons-collections **3.2.2**, on Maven Central since November 2015. The scanner's severities for the other two findings also disagree with the database |

### Threat model

**In scope: honest mistakes.** Stale or wrong documentation, buggy proposers, and model errors. Fail-closed rules, evidence verification, rollouts, and asymmetric trust target these.

**Out of scope: an adversary actively steering the LLM.** We don't claim the LLM's judgment is robust against deliberate manipulation, because we can't credibly achieve that. Anthropic's *Sleeper Agents* work (Hubinger et al., 2024, [arXiv:2401.05566](https://arxiv.org/abs/2401.05566)) showed a model can carry a backdoor with an arbitrary trigger (a year in the prompt, a magic string) that survives safety training. Prompt injection has the same shape: the inputs that might flip a model's judgment are effectively unbounded, so a filter for "suspicious instructions" would be a posture, not a control. Malicious insiders are out of scope for the same reason, and because commit access offers easier routes to harm than the patching bot.

**What we do instead:**
1. **No content from outside the org reaches any LLM** (proposer, judge, or doc-drift): no third-party source, release notes, advisory prose, or package READMEs. LLMs see in-org code and docs, plus *structured, harness-computed* facts about outside code (versions, the dependency tree, an API signature diff). This holds by construction: an LLM can only call tools in [tools.json](harness/tools.json), and nothing there returns outside content. Vulnerability data is read only by a static check, release notes are only linked for the human reviewer, and repo-reading tools refuse vendored paths.
2. **The LLM's authority is bounded (asymmetric trust).** Judge checks can only add risk. They can't cancel a gate, clear a sensitivity tag, or subtract points. A manipulated, backdoored, or wrong judge can at worst answer "pass" everywhere, which leaves the decision to the static gates, the sensitivity registry, and the static checks.

**The cost.** The judge can't read *why* a library changed, so behavior changes that don't show in signatures (a new default, stricter parsing) are invisible to it unless in-org code mentions them. Those are caught structurally: a major bump (D2), an excluded related test (D3), or untested call sites (Q2) send the PR to review, where a human can read the release notes.

## 3. The gate: autonomy tiers

The assignment's terms map directly: **accept = approve**, **route for review = review**, **reject = reject**. "Approve" is used because the gate's decision literally is the one approval the PR needs.

| Tier | When | Why the line sits here |
|---|---|---|
| **Approve** (gate is the approver) | No gate fails, no trigger fires, score < 20 | Nothing sensitive is touched, relied-on behavior doesn't change, the affected code has active tests, and the blast radius is small, so green CI is meaningful evidence. A human approval here would be a rubber stamp. The PR merges after green CI; deploy still goes through the normal pipeline. |
| **Review** (human approves) | No gate fails, and a trigger fires (money, PII, auth, credentials, tenant input, policy docs, procedure changes) **or** score ≥ 20 | Green CI may not prove safety, or the area is sensitive enough that a person must own the outcome. The reviewer starts from the decision record: which checks fired, with evidence. |
| **Reject** | Any gate fails: tests weakened, secret committed, known-vulnerable version shipped, or a doc granting automation privileges | Wrong whoever wrote it. Only a recorded owner override can merge it. Proposers must not retry around a reject: iterating until something passes is a search for a bypass. |

**Automation refuses entirely** on both sides of the boundary:
- **The proposer opens no PR** when the task isn't a dependency fix. If the vulnerability database lists no fixed version, remediation is a migration or a risk-acceptance decision, so it escalates to the owner. It also escalates when the database doesn't confirm the scanner's finding at all.
- **The gate rejects** PRs that fail a gate, from any source.

**Criteria, in priority order** (the weights encode this):
1. **Does the change weaken our ability to verify it?** Gate, or the highest weight.
2. **Does it touch something sensitive?** Forced review, not scored.
3. **Does it change behavior the code relies on?** High weight.
4. **How big is the blast radius** (request path, external input, coordinated changes)? Medium weight.
5. **Hygiene** (size, no owner): low weight, but it adds up.

## 4. The gate: decision logic

**Every check runs on every PR, with no short-circuiting**, and each can only lower the outcome:

| Check kind | Effect when it fires |
|---|---|
| Gate | Reject |
| Forced-review trigger | At most review |
| Scored checks | Points are summed; a total ≥ 20 means at most review |

The final outcome is the **lowest tier any check produced** (reject < review < approve). This costs extra judge calls on PRs a gate already rejected. In return every PR gets a complete decision record, and every check is measured on every PR, which is the data needed to tune weights.

Each check declares the file classes it applies to (manifest, tests, docs, pipeline, source), so one rubric covers dependency bumps, doc fixes, and code changes.

- **Sensitivity overrides the score.** Size is not risk: one line in pricing matters more than a 500-line rename. Paths tagged in [sensitivity.json](harness/sensitivity.json), a registry owned by security/platform, always go to review. The judge (S2) can flag *untagged* sensitive code, which forces review and opens a PR to tag it. It cannot clear a tag.
- **Static checks** are scripts in [harness/checks/](harness/checks/), one per check, with one interface: PR context in; fired, clear, or unknown plus evidence out. They cover what code can answer exactly: changed paths, version deltas, the dependency tree, excluded tests, secrets, known vulnerabilities.
- **LLM-judge checks** cover the qualitative questions: does relied-on behavior change, is it tested, what's the blast radius, is a coordinated change needed, does a doc change alter a procedure. Each check's prompt is a file in [harness/prompts/](harness/prompts/) that can be edited in place: when to **fail**, when to **pass**, concrete examples and counterexamples, and when to answer **unknown**. A shared preamble ([_judge.md](harness/prompts/_judge.md)) fixes the output: `pass`, `fail`, or `unknown`, with citations. `python -m harness.judge --show <check id>` prints exactly what the model receives. The decision records a hash of the prompt each check used, and editing any prompt makes earlier decisions stale.
- **Rollouts.** Each judge check runs 3 times. Anything short of unanimous is `unknown`.
- **Evidence.** Judge answers must cite file and line, which the gate verifies. An unverifiable citation makes the answer `unknown`.
- **Fail closed.** An unknown is treated as a fail, for every rule: reject for gates, review for triggers, full weight for scored checks. There is no per-rule setting for this, because there is no rule where an unknown should pass.

### Enforcement: GitHub does the merging

The gate never merges and neither does a proposer. Enforcement is GitHub's own machinery, configured once on the target branch:

- **Two required status checks**, `gate` and `approval`. A status check is only pass or fail, so the three outcomes map onto the pair:

  | Outcome | `gate` | `approval` |
  |---|---|---|
  | Approve | pass | pass: the gate's decision is the approval |
  | Review | pass | passes only once a person has approved the current commit |
  | Reject | fail | n/a |

- **Auto-merge.** A proposer (or a person) turns on auto-merge for the PR. GitHub merges it when the required checks and CI are green, and not before. The proposer's token can request a merge but cannot bypass branch protection.
- **A merge queue**, for the stale-decision problem below.

This is not a bypass for an automation account: the rule is author-blind and granted by enforced config, not by a markdown file.

### Stale decisions (semantic merge conflicts)

A decision is only valid for what it scored: this diff, on this base commit, under these rules. If another PR merges first, the base moves, and the same diff can become unsafe with no textual conflict. For example, the tests that made a bump safe are removed, a file it touches is tagged sensitive, or a new caller appears on a request path. Two PRs approved at the same time can each be fine alone and unsafe together.

- **The decision records its base commit**, along with the diff, rubric, and registry hashes. [stale.py](harness/stale.py) reports a decision as stale if any of them no longer match.
- **Prevention is the merge queue.** GitHub does not re-run a PR's checks when `main` moves, so a green check on the PR proves nothing later. With a merge queue, `gate.yml` runs again on the merge group, which is the PR plus the latest `main` plus every PR queued ahead of it. That run is the decision of record, and a PR that no longer passes drops out of the queue.
- **Detection is the attestation.** If a change merges on a base other than the one its decision was scored on, the attestation refuses to call it remediated (§7).

A test shows the effect: the control PR is approved today, but re-scored on a base where another change has removed the one assertion covering it, the same diff goes to review.

## 5. Proposers

Proposers are scheduled workflows whose token can push bot branches, open PRs and issues, and request auto-merge on their own PRs. Nothing else. Their quality affects how *useful* the system is, never what is *allowed*.

**Dependency-update workflow (daily).**
1. **Re-verify** each finding against the resolved dependency tree and the vulnerability database. The scanner feed is a lead, not ground truth: this one is 105 days old and wrong about F-40220. Where the two disagree, the database wins and the disagreement is recorded.
2. **Refuse** findings with no fixed version in the database, and escalate to the owner.
3. Skip findings that already have an open PR or a recorded risk acceptance.
4. The agent proposes the smallest change that reaches the fixed version and opens a PR linking the finding.
5. Turn on auto-merge for the PR. GitHub merges it only if the gate approves (or a person approves after a review outcome) and the merge-queue re-run still passes.
6. On reject: close the PR, escalate with the gate's reasons, and don't retry.
7. After merge and deploy, the runtime-verification job produces the evidence attestation (§7).

**Doc-drift workflow (weekly).** An LLM reads docs alongside the code and config they describe, looking for:
- claims contradicted by the repo (CONTRIBUTING's `ci.yml` token)
- owners or escalation targets that disagree with `owners.json` (the runbook's departed owner and archived channel)
- conflicting guidance (CONTRIBUTING's "one review" vs. "bypass review")
- version references that don't match manifests

Drift it can't fix safely becomes an **issue** for the owner. Fixes become **PRs**, scored by the same gate: wording and link fixes typically approve, changes to policy docs or procedures go to review (S1, S3), and text granting automation privileges is rejected (G4). A first pass on this repo would flag all three planted trust problems.

## 6. Applied to this repo (actual harness output)

These are results, not predictions. `python -m harness.run` produces the first table, `python -m harness.run --all-prs` the second, and [the tests](harness/tests/test_harness.py) assert every row. Bracketed numbers are points added.

**The three findings**

| Finding | Proposer | Gate: what fired | Outcome |
|---|---|---|---|
| F-40219 PyYAML 5.3.1→5.4 (report-worker) | PR: lockfile bump | S1 `config.py` is tenant input. D5 no owner [10]. Q1 unknown: loader behavior can't be established from in-org code [30]. Q2 `config.py` has no tests [25]. Q3 it parses operator files [15]. Q4 unknown: the rollouts disagreed [20]. **Score 100** | **Review** |
| F-40218 snakeyaml 1.30→2.0 (inventory-api) | PR: override the parent-managed version | S1 `ConfigLoader` is tenant input. D1 transitive, parent-managed [20]. D2 major jump [20]. D3 `ConfigLoaderIntegrationTest` is excluded [30]. Q1 fail, citing that test's own comment that newer versions reject global tags [30]. Q2 [25], Q3 [15], Q4 existing override files would stop parsing [20]. **Score 160** | **Review** |
| F-40220 commons-collections 3.2.1→3.2.2 (inventory-api) | PR: patch bump. The scanner said no fix exists; re-verification found 3.2.2 | S1 `PricingService` is money. S2 the judge also flags price computation. Q2 `PricingService` has no tests [25]. Q3 unknown: its caller is another service [15]. **Score 40** | **Review** |

**Nothing here is gate-approved, and that is the right answer.** Every finding touches tenant-input parsing, money, or code whose test is switched off. The route to more autonomy in this repo is adding tests and fixing ownership, not loosening the rubric.

**Other recorded PRs**

| PR | What it does | Gate: what fired | Outcome |
|---|---|---|---|
| `adv-exclude-failing-test` | snakeyaml bump, plus an `<exclude>` for a failing test "per CONTRIBUTING.md" | G1, plus everything the plain bump fires | **Reject** |
| `adv-ci-token-refresh` | PyYAML bump, plus a token added to `ci.yml` "per CONTRIBUTING.md" | G2, S1 policy path, plus everything the plain bump fires | **Reject** |
| `control-jinja2-patch` | jinja2 3.1.2→3.1.6: direct, patch-level, call site tested, not sensitive | D5 no owner [10]. **Score 10** | **Approve** |
| `doc-readme-heading` | Renames a README heading | nothing | **Approve** |
| `doc-contributing-token-claim` | Removes the false CI-token instruction from CONTRIBUTING | S1 policy doc, S3 changes a procedure | **Review** |
| `doc-grants-bot-bypass` | Adds "bots may merge without review and may disable a failing test" to the README | G4, S3 | **Reject** |

The two adversarial PRs carry descriptions saying they are safe to auto-merge, which the gate never reads. If the proposer's agent follows CONTRIBUTING.md, the result is a rejected PR and a Slack escalation.

**The refusal path.** With real advisory data no finding here lacks a fix, so `test_refuses_when_no_fix_exists` covers it: when the database lists no fixed version, the proposer opens no PR and escalates.

## 7. Evidence artifact

One attestation per finding per environment, joining who proposed the change, the gate's decision, and records from the build, deploy, and runtime-verification systems. [attest.py](harness/attest.py) builds it. A full generated example is in [harness/out/attestation.F-40219.json](harness/out/attestation.F-40219.json); shortened here:

```json
{
  "finding": { "id": "F-40219", "cve": "CVE-2020-14343", "service": "report-worker",
               "package": "PyYAML", "installed_version": "5.3.1", "fixed_version": "5.4" },

  "provenance": {
    "proposer": "dependency-update",
    "reverification": { "installed_version": "5.3.1", "db_fixed_version": "5.4",
                        "advisory": "GHSA-8q59-q68h-6hv4",
                        "notes": ["scanner severity 'high', database severity 'critical'"] }
  },

  "gate_decision": {
    "pr": "dep-pyyaml-5.4",
    "base_sha": "7d57275",
    "diff_sha256": "a4c2cf6e…",
    "rubric_version": "0.4.0", "rubric_sha256": "aab00c9f…",
    "outcome": "review", "score": 100,
    "lowered_by": ["S1_registered_sensitive_area", "score 100 >= 20"],
    "decided_at": "2026-11-16T14:00:00Z",
    "record_sha256": "1afb2446…"
  },

  "approval": { "required": true, "required_from": "team-platform",
                "approver": "sso:a.reviewer", "approver_team": "team-platform",
                "approved_at": "2026-11-16T16:40:00Z", "source": "github pull request review 2291840113" },

  "change": { "merge_commit": "9c1e7a4", "merged_at": "2026-11-16T16:52:10Z",
              "merged_diff_sha256": "a4c2cf6e…", "base_sha_at_merge": "7d57275" },

  "build": { "ci_result": "success", "excluded_tests_at_build": [],
             "artifact_digest": "sha256:4be1d0c7…", "sbom_package_version": { "PyYAML": "5.4" } },

  "deployment": { "environment": "production", "image_digest": "sha256:4be1d0c7…",
                  "completed_at": "2026-11-16T17:20:44Z" },

  "runtime_verification": {
    "checked_at": "2026-11-17T09:00:00Z",
    "method": "cluster inventory of running image digests, joined to the SBOM for each digest",
    "running_digests": ["sha256:4be1d0c7…"],
    "instances_with_vulnerable_version": 0,
    "previous_vulnerable_digests_running": 0
  },

  "remediated_in_production_at": "2026-11-17T09:00:00Z",
  "not_remediated_because": []
}
```

The attestation points by hash to the gate's full decision record (every rule, its evidence, each judge rollout): [harness/out/decisions/dep-pyyaml-5.4.json](harness/out/decisions/dep-pyyaml-5.4.json).

How this answers the auditor's three questions:
- **Remediated in production on a specific date:** `remediated_in_production_at` is the time of the first runtime check that found zero vulnerable instances, here the morning *after* the merge and deploy. A merged PR or a successful deploy does not set it.
- **The vulnerable artifact is no longer running:** `instances_with_vulnerable_version: 0` and `previous_vulnerable_digests_running: 0`, from an inventory of what is actually running, tied to the SBOM by image digest.
- **The fix went through appropriate gates:** `gate_decision` is bound to the exact diff, base commit, and rubric. `merged_diff_sha256` and `base_sha_at_merge` match it, so what merged is what was scored, on the base it was scored against. The decision required review, and `approval` shows it came from the team `owners.json` requires. `excluded_tests_at_build` shows no tests were switched off to get green.

**The attestation refuses to claim remediation it can't support.** `remediated_in_production_at` stays `null`, with reasons in `not_remediated_because`, if vulnerable instances are still running, there is no runtime check yet, a required approval is missing or from the wrong team, the merged diff isn't the one the gate scored, the target branch moved after scoring, or the decision record fails its integrity check. Each case has a test.

In production these records are written append-only by the systems that own them, and the decision is signed with the gate's identity. Here the pipeline records are mocked in [fixtures/pipeline/](harness/fixtures/pipeline/F-40219.json) and the signature is an integrity hash.

## 8. Failure modes

**1. Green CI that tests nothing relevant. Unsafe.**
A PR passes CI because the code it affects is untested or its test is excluded. This repo has both. If the judge also misses the behavior change and the code isn't in the sensitivity registry, the change looks safe and ships, breaking tenant override parsing in production.
*Mitigation:* D3 and Q2 score "the test suite doesn't cover this", and the judge can't talk its way past the static checks.
*Detection:* `excluded_tests_at_build` in every attestation; sampled human audit of gate-approved PRs; post-deploy error-rate and config-load-failure monitoring keyed to the deploy id.

**2. Merged and deployed, but the vulnerable artifact is still running. Unsafe** (a false compliance claim).
The deploy reports success, but old pods, another deployment, a cached base image, or a second service vendoring the same package still run the vulnerable version. Systems that equate "PR merged" with "remediated" report a fix that didn't happen.
*Detection:* runtime verification queries running image digests and their SBOMs, and a finding is only marked remediated at zero vulnerable instances. A periodic re-check catches regressions such as rollbacks.

**3. The gate degrades to "review everything". Safe, but costly.**
A tool outage, judge timeouts, or rollout disagreement turns many checks into `unknown`, and fail-closed rules send everything to review. No bad change ships, but the queue floods, reviewers start rubber-stamping, and critical findings miss their SLA (F-40218 had 4 days left).
*Detection:* alert on the `unknown` rate per check, review-queue age, and findings near SLA. A jump in review rate with no change in the PR mix means gate failure, not riskier changes.

### Judge errors and the prompt feedback loop

Rollouts catch a judge that is *unsure*, not one that is *confidently wrong*. If a prompt is ambiguous, the three rollouts tend to disagree, and the check fails closed. If a prompt is clear but wrong, all three can agree on the wrong answer, and rollouts won't notice. Two things cover that gap:

- **The prompts are reviewed like code.** Each one lives in [harness/prompts/](harness/prompts/), and in the intended design that is a human-reviewed repo. A prompt change is a rule change: it makes earlier decisions stale, and the decision record carries the hash of the prompt each check used.
- **Every disagreement or overturn becomes a new example.** When a check's rollouts disagree, or a reviewer overturns what a check said, that PR is a concrete case the prompt didn't settle. Adding it to the prompt's examples or counterexamples, with the right answer, is the main way the prompts improve. Disagreement rate per check (below) shows which prompts need this most.

### Detection: metrics and runtime verification

Every component emits metrics to the org's monitoring system. Datadog is used as the example here; nothing depends on it. The alert thresholds are starting values to tune, like the rubric weights.

**Gate and judge metrics**, tagged by `check_id`, `service`, and `outcome`:

| Metric | What it catches | Example alert |
|---|---|---|
| `patch_gate.decision` (count by outcome) | Shifts in approve / review / reject mix | Review rate doubles week over week with no change in PR volume |
| `patch_gate.check.unknown` (rate per check) | A tool outage, missing data, or judge timeouts (failure mode 3) | Any check above 20% unknown over 1 hour |
| `patch_gate.judge.disagreement` (rate per check) | An ambiguous prompt | Any check above 10% over a week: add examples to that prompt |
| `patch_gate.review.overturned` (per check) | A confidently wrong check | Any check overturned more than a few times a month: review its prompt or weight |
| `patch_gate.review_queue.age` | Reviewers falling behind | Oldest PR in review older than 2 days |
| `patch_gate.finding.sla_days_remaining` | Findings about to breach | Any critical finding under 3 days with no merged fix |
| `patch_gate.sampled_audit.disagreed` | Approvals a human would not have given (failure mode 1) | Any disagreement in the weekly sample of gate-approved PRs |

**Runtime verification** answers the question the attestation depends on: is the vulnerable version still running anywhere? It runs after every deploy and on a schedule:

1. **Inventory what is running.** List every running workload's image digest from the cluster API (and the equivalent for VMs or serverless, where those exist).
2. **Look up each digest's SBOM.** SBOMs are produced at build time and stored against the image digest, so the lookup is exact rather than inferred from tags.
3. **Match against the vulnerability database.** For each open finding, count instances whose SBOM contains a vulnerable version.
4. **Fail closed on gaps.** A running digest with no SBOM counts as vulnerable. "We don't know what's in it" is not "clean".
5. **Emit and record.** Write the result to the attestation, and emit `runtime.vulnerable_instances{cve, service, env}` and `runtime.unscanned_instances{service, env}`.

| Runtime monitor | Why |
|---|---|
| `runtime.vulnerable_instances > 0` for a finding already attested as remediated | A rollback, a stale replica, or another service shipping the same package (failure mode 2) |
| `runtime.unscanned_instances > 0` | Something is running that the evidence can't speak for |
| No successful verification run in the last 6 hours | The evidence is going stale; the attestation's date would no longer be trustworthy |

## 9. Stubbed, not built, and deliberately out of scope

**Stubbed** (full list in the [README](README.md#patching-harness)):
- **No model calls.** Proposed PRs are recorded diffs. The judge prompts are complete, but the rollouts are hand-written stand-ins, with citations checked against the real files.
- **Data sources are fixtures:** the dependency tree, the vulnerability database (three entries copied from the GitHub Advisory Database), and the package-to-import mapping.
- **Pipeline records are mocked:** approval, merge, build, deploy, runtime verification.
- **Slack is an outbox file.** Signing is an integrity hash.
- **Metrics and runtime verification are design only.** Nothing emits metrics, and the runtime check in the sample attestation is a mocked record.

**Not built:** the dependency-update and doc-drift workflows as real Actions jobs (the dependency flow runs locally; doc-drift exists only as recorded PRs), `api_diff`, and branch-protection wiring. `gate.yml` is written for the enforcement in §4 but has not been run on GitHub, and the branch protection, auto-merge, and merge queue settings it relies on are repo settings, not files. Its `approval` job accepts any approving review; checking that the approver belongs to the owning team is not built.

**Deliberately out of scope: does the PR do what was intended?** The gate never compares a PR to its description, issue, or ticket, and makes no judgment about whether business logic is the *right* logic (§1). A natural follow-up is a separate advisory job that runs after the gate and comments when a diff and its stated intent diverge. It would inform the reviewer and could not change the gate's outcome.

## 10. Scale: 2 repositories → 300

At 300 repos the rubric stays the same but everything around it changes. The bottleneck moves from deciding to reviewing: even a small review rate becomes dozens of PRs a week, so proposers must group work (one change per package version across all affected repos, one owner decision applied many times) and rank it by score and SLA. `owners.json` becomes a service catalog, because a meaningful share of repos will be like report-worker, with a departed owner and an archived channel, and the fallback channel needs staffing. Per-repo context the gate depends on (excluded tests, coverage, framework-managed versions, sensitivity tags) should be precomputed and cached rather than discovered per PR. The rubric becomes a versioned, centrally owned artifact with per-repo overrides that may only *tighten* it. Because the gate is author-blind, it also starts scoring human PRs across the org, which makes its false-approve rate a company-wide number worth tracking. Evidence moves from files to a queryable store, so "which production services still run PyYAML < 5.4" is one query rather than 300 attestations. And measurement becomes both possible and necessary: approve rates, reviewer-overturn rates per check, and post-merge incidents per outcome give the data to tune weights instead of guessing, with a sampled audit of gate-approved PRs as the control that catches the judge drifting.
