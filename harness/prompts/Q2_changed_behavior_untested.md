# Check: is the behavior this change could affect left untested?

Consider each changed function, and each in-org call site of a changed dependency. For each,
find an ACTIVE test (not excluded, skipped, or disabled) whose ASSERTIONS would fail if that
behavior changed. A test that only executes the code without checking the result does not count.

## FAIL if

At least one changed function or call site has no active test asserting on its output or
side effects.

## PASS if

Every one has an active test that asserts on its behavior. Cite the assertion line.

## Examples: FAIL

- `config.py` calls `yaml.load`, and the only test module imports `render`, not `config`.
- The only test of `loadOverrides` is listed in `<excludes>` in the pom.
- A test calls `priceFor(...)` but never asserts on the result.
- The test is marked `@Disabled` or `pytest.mark.skip`.

## Counterexamples: PASS

- `render()` is the only call site of the changed library, and
  `test_render_includes_tenant` asserts `"SKU-1001: 42" in out`.
- Each changed function has a test asserting its return value for normal and error input.

## UNKNOWN if

You can't tell whether a test reaches the call site, for example it goes through a
framework entry point you can't trace with the tools you have.
