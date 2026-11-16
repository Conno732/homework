# Check: is the blast radius large?

Consider each changed function, and each in-org call site of a changed dependency.

## FAIL if any of them

- is reachable from an HTTP handler, message consumer, worker loop, or scheduled job, or
- processes data that comes from outside this service's own build: user requests,
  tenant or operator uploads, files in a shared bucket, another service's messages

## PASS if

All of them are build-time only, startup-only on config the service's own pipeline writes,
or tooling and tests.

## Examples: FAIL

- `load_override(path)` parses files operators drop into a tenant bucket.
- A `@GetMapping` handler calls the changed code.
- A queue consumer deserializes messages with the changed library.

## Counterexamples: PASS

- A code generator run during the build.
- Reading `config.yaml` written by the service's own deploy pipeline, once at startup.
- `render()` with a constant template and no caller outside tests in this repo.

## UNKNOWN if

The code is public but has no caller in this repo, and a comment says another service uses
it. You can't see the other service.
