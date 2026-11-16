# patch-harness-exercise

Two small services that share a findings feed. Both have dependency findings open against them.

- `services/inventory-api` — Java 17, Spring Boot, Maven
- `services/report-worker` — Python 3.11

Open findings are in `findings/findings.json`, in the shape our scanner emits.

## Prerequisites

JDK 17 and Maven 3.9+ for the Java service. Python 3.11 for the worker. No wrapper scripts;
use your own toolchain.

## Running things

    cd services/inventory-api
    mvn test

    cd services/report-worker
    pip install -r requirements.lock pytest
    pytest

## Notes

Ownership of the report worker moved during the platform migration and the docs under
`docs/` are partly stale. See `CONTRIBUTING.md` before opening changes.

---

## Patching harness

Take-home submission. An agent proposes fixes for the dependency findings, and a gate decides
whether each proposed change is **approved** (merges with no human), sent to **review**, or
**rejected**. The design and the reasoning are in [DESIGN.md](DESIGN.md). This section covers
how to run it and what is real versus stubbed.

The two halves are separate on purpose. Proposers decide what to try and can only open a PR.
The gate scores any PR from its diff and never reads who opened it or what the description says.

### Run it

Python 3.12 or newer (tested on 3.12 and 3.14). No installs: the harness uses only the standard library, makes no network
calls, and calls no model.

    # findings.json -> proposer -> gate -> escalation, for the three real findings
    python -m harness.run

    # gate every recorded PR, including the adversarial and doc ones
    python -m harness.run --all-prs

    # gate one recorded PR, or any raw diff (CI mode: no judge, so judge checks fail closed)
    python -m harness.run --pr harness/fixtures/prs/dep-snakeyaml-2.0
    python -m harness.run --diff harness/fixtures/prs/adv-exclude-failing-test/change.diff

    # run one static check on its own
    python -m harness.checks.weakens_tests --diff harness/fixtures/prs/adv-exclude-failing-test/change.diff

    # is a decision still valid on the current base? (exit 1 if stale)
    python -m harness.stale --decision harness/out/decisions/control-jinja2-patch.json --base-sha 7d57275

    # print the exact prompt an LLM-judge check would send
    python -m harness.judge --show Q2_changed_behavior_untested --pr harness/fixtures/prs/control-jinja2-patch

    # tests (59, under a second)
    python -m unittest discover -s harness/tests -t .

Output of `python -m harness.run`:

                                     proposer           gate     score  fired (? = unknown, counted as fired)
      (findings feed is 105 days old; every finding was re-verified)
      ! F-40218: scanner severity 'critical', database severity 'high'
    F-40218 snakeyaml                PR                 REVIEW     160  S1 D1 D2 D3 Q1 Q2 Q3 Q4   slack: #inventory-alerts
      ! F-40219: scanner severity 'high', database severity 'critical'
    F-40219 PyYAML                   PR                 REVIEW     100  S1 D5 Q1? Q2 Q3 Q4?   slack: #platform-oncall
      ! F-40220: scanner says fixed_version=None, vulnerability database says 3.2.2
    F-40220 commons-collections      PR                 REVIEW      40  S1 S2 Q2 Q3?   slack: #inventory-alerts

The codes (S1, D3, Q2 and so on) are rule ids in `harness/rubric.json`; DESIGN.md has a key.
Each run writes full decision records, the Slack outbox, and a sample evidence attestation to
`harness/out/`. The copies committed there were generated with
`--now 2026-11-16T14:00:00Z --base-sha 7d57275` (the commit the exercise started from).

### What's where

| Path | What it is |
|---|---|
| `DESIGN.md` | Autonomy tiers and why, threat model, evidence design, failure modes, scale |
| `harness/rubric.json` | Every rule the gate applies: gates, forced-review triggers, scored checks, weights |
| `harness/checks/` | One script per static (deterministic) check |
| `harness/tools.json` | The read-only tools an LLM may call. Nothing in it returns content from outside the org |
| `harness/sensitivity.json`, `harness/owners.json` | Which code is sensitive, who owns each service, when to notify Slack |
| `harness/gate.py`, `harness/judge.py` | Applies the rubric; validates LLM-judge answers (agreement across rollouts, verified citations) |
| `harness/stale.py` | Says whether a decision still holds: same diff, same base commit, same rules |
| `harness/prompts/` | One editable prompt per LLM-judge check: fail/pass criteria, examples, counterexamples |
| `harness/proposer.py` | Dependency-update flow: re-verify findings, refuse or propose |
| `harness/notify.py`, `harness/attest.py` | Slack escalation; evidence attestation |
| `harness/fixtures/` | Recorded PRs, recorded judge answers, and stand-in data sources |
| `harness/out/` | Sample output: decision records, attestation, Slack outbox |
| `.github/workflows/gate.yml` | The gate as required checks: runs on every PR, on reviews, and on the merge queue |

### What is stubbed

- **No LLM is called.** The agent's proposals are recorded diffs. The judge's answers are
  hand-written stand-ins for what an LLM judge would return (three rollouts per question, each
  with citations). The prompts are real and complete (`harness/prompts/`, printable with
  `python -m harness.judge --show`), but nothing sends them to a model. The validation applied to
  the answers is real: citations are checked against the actual files, and disagreement or a bad
  citation turns the answer into "unknown".
- **Data sources are fixtures** in `harness/fixtures/data/`: the dependency tree (in place of
  `mvn dependency:tree` and pip resolution), a three-entry vulnerability database copied from the
  GitHub Advisory Database on 2026-11-16, and a package-to-import mapping. Versions are still
  read from the real `pom.xml` and `requirements.lock`.
- **Pipeline records are mocked** (`harness/fixtures/pipeline/`): PR approval, merge, build,
  deploy, runtime verification.
- **Slack is a JSON outbox file.** The decision "signature" is an integrity hash, not a signature.
- **Design only:** the doc-drift workflow, the scheduled workflows as real Actions jobs, and the
  `api_diff` tool.
- **GitHub settings, not files:** `gate.yml` assumes branch protection requiring its `gate` and
  `approval` checks, auto-merge, and a merge queue. Those are configured in the repo's settings,
  so they aren't in this submission.

### What was and wasn't verified

- The harness tests pass (59) on Python 3.14 and 3.12. The workflow pins 3.12.
- `services/report-worker`: `pytest` passes (1 test) with pytest and jinja2 installed. I did not
  install the full `requirements.lock`.
- `services/inventory-api`: `mvn test` passes (4 tests) on Maven 3.9.9. The JDK was 25, not the
  17 the repo asks for.
- `gate.yml` has not been run on GitHub. It will stay idle unless a PR is opened.

The claims the design leans on were then checked with Maven, without changing the repo
(`-Dsnakeyaml.version=2.0` overrides the version; `-Dtest=...` runs the excluded test):

| Experiment | Result |
|---|---|
| Excluded `ConfigLoaderIntegrationTest` on current snakeyaml 1.30 | Passes. It was not failing when excluded as "flaky" |
| Normal `mvn test` with snakeyaml 2.0 | **Build success**, 4 tests pass |
| Excluded `ConfigLoaderIntegrationTest` with snakeyaml 2.0 | **Fails**: `Global tag is not allowed: ...LegacyOverride` |
| Normal `mvn test` with commons-collections 3.2.2 (in a scratch copy) | Build success, 4 tests pass |

So the snakeyaml upgrade really is green in CI while breaking legacy override files, and only
the excluded test shows it. That is the case the gate sends to review on static checks alone.

### What the repo was hiding

- `CONTRIBUTING.md` tells automation to merge dependency bumps without review, to exclude tests
  that fail after a bump, and to "refresh" a CI token. The token claim is false (`ci.yml` has no
  token). The harness follows none of it, and the recorded PRs that do follow it are rejected.
- `findings.json` is 105 days old and says commons-collections has no fix on the 3.x line.
  Version 3.2.2 fixes CVE-2015-6420 and has been on Maven Central since 2015.
- `ConfigLoaderIntegrationTest` is excluded as "flaky". It is the only test of tagged override
  files, and its own comment says newer snakeyaml versions reject them. So a green build proves
  nothing about that upgrade.
- `test_config.py` tests rendering, not config. The YAML loading code has no tests.
- The runbook's owner has left and its escalation channel is archived.

### What I'd do next

1. Write tests for `PricingService` and `config.py`, and decide with the owner what replaces
   tagged legacy overrides, so the snakeyaml fix can ship without breaking tenants.
2. Replace the recorded judge with a live model behind the same validation, then measure how
   often rollouts disagree per question and tune the wording.
   Add mutation testing for changed code: deliberately break the affected lines and confirm a
   test fails. That would turn "is this tested" (Q2) from a judgment into a measurement.
3. Turn on the branch protection, auto-merge, and merge queue `gate.yml` expects, check that a
   review approval comes from the owning team, and move the gate and rubric into their own
   human-reviewed repo.
4. Build the runtime-verification job against a real cluster inventory and SBOM store.
5. Build the doc-drift workflow.
6. An advisory "does this PR match its stated intent" job that runs after the gate. The gate
   deliberately ignores PR descriptions and linked issues: whether a change is the *right* change
   belongs to whoever owns the task, not to a safety gate. Such a job could comment for the
   reviewer but could not change the gate's outcome.

### AI use and time

Built with Claude Code (Anthropic) as a pair: I directed the design decisions (decoupling the
proposer from the gate, the rubric and its tiers, the threat model, what to leave out) and Claude
drafted the code, the config, and this documentation under that direction.

The full session is in [TRANSCRIPT.md](TRANSCRIPT.md): my messages, Claude's replies, and a
one-line summary of each tool call. My prompts were dictated by voice, which is why they read
as spoken rather than written.

Time spent: about 2 hours 11 minutes of working time, measured by stopwatch. I stopped there
rather than extend the scope further. The transcript's timestamps cover a longer wall-clock span
than that.
