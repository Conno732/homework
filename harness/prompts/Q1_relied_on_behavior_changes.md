# Check: does the change alter behavior that existing code relies on?

Consider changed functions, and every in-org call site of a changed dependency. For a
dependency, `api_diff` lists removed and changed public signatures between the old and new
version. You do not have the library's release notes or source. Judge only from `api_diff`
and from what in-org code, tests, and comments say.

## FAIL if

- a call site uses a symbol `api_diff` lists as removed or changed, or
- in-org code, a test, or a comment documents a behavior that the change alters (a default,
  accepted input, parsing rule, error behavior), and some call site depends on it, or
- a changed function's return value, accepted input, or error behavior changes and an
  existing caller depends on the old behavior

## PASS if

Every call site uses only symbols `api_diff` shows as unchanged, and no in-org evidence
points to a relied-on behavior change.

## Examples: FAIL

- `api_diff` lists `Yaml.load(InputStream)` as signature-changed, and `ConfigLoader` calls it.
- A test comment reads "newer parser versions reject global tags by default", and the
  change upgrades the parser while `loadOverrides` depends on global tags.
- A function used to return `null` for a missing key and now throws, and a caller checks for `null`.

## Counterexamples: PASS

- `api_diff` shows no change to `Template` or `Template.render`, and those are the only
  symbols used.
- A changed function gains an optional parameter with the old default, and callers don't pass it.
- Only internal (private) code changed, and every caller is in the same diff and updated.

## UNKNOWN if

`api_diff` shows nothing relevant and nothing in-org documents the behavior either way,
for example a loader's handling of edge-case input. Don't infer library behavior from memory.
