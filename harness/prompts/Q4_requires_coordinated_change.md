# Check: does this change only work if something outside the diff also changes?

## FAIL if, after merging only this diff, any of these would be rejected or read differently

- input the code accepts today: config files, serialized data, tag or syntax forms, API requests
- data at rest: stored records, caches, files in buckets
- consumers of this code's output: other services, reports, clients

Cite the code that reads or produces the affected format.

## PASS if

Every existing input, stored datum, and consumer stays valid with identical meaning.

## Examples: FAIL

- Override files in a tenant bucket use `!!com.example.LegacyOverride` tags, and the change
  makes the parser reject global tags.
- A field is renamed in a JSON response another service reads.
- A serialization library upgrade changes the on-disk format of cached objects.

## Counterexamples: PASS

- A template library patch release where the template and its output are unchanged.
- An in-memory data structure swapped for an equivalent one, never serialized.
- A new optional config key with the old behavior as the default.

## UNKNOWN if

Whether existing inputs are affected depends on data you can't see, for example whether any
override file in the bucket actually uses tags.
