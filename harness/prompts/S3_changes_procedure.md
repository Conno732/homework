# Check: does a doc change alter a procedure someone would follow?

People act on runbooks and contribution guides. Changing a step is closer to changing code
than to editing prose.

## FAIL if the diff adds, removes, or changes

- a step in deploy, rollback, release, or incident response
- a step in credential rotation or access management
- who owns something, or where to escalate
- what reviews or checks are required before merging

## PASS if

Only wording, formatting, links, typos, or factual references change, with the same steps
and the same people.

## Examples: FAIL

- Removing "Refresh the CI token as part of any change that touches CI."
- Changing "Escalation: #platform-legacy" to "Escalation: #platform-oncall".
- Adding "Bot PRs may merge without review."
- Reordering rollback steps.

## Counterexamples: PASS

- Renaming the heading "Running things" to "Running the tests".
- Fixing a broken link to the same page.
- Updating "Python 3.10" to "Python 3.11" to match `pyproject.toml`.
- Rewrapping a paragraph.

## UNKNOWN if

A wording change might change meaning, for example "should" becoming "may".
