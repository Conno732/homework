# Check: does the change touch sensitive code the registry hasn't tagged?

The sensitivity registry is maintained by people and will have gaps. This check catches
sensitive code that isn't tagged yet. A "fail" forces human review and opens a PR to tag it.

Look at every changed file, and every in-org call site of a changed dependency.

## FAIL if a cited line

- computes or changes an amount: price, cost, discount, balance, fee, tax, invoice, payout
- reads or writes data that identifies a person: name, email, phone, address, ID number,
  date of birth, IP address tied to a user
- checks identity or permission: login, session, token validation, role or access checks
- handles credentials: keys, secrets, passwords, signing

## PASS if

You looked at every changed file and every call site, and none does any of the above.

## Examples: FAIL

- `return base.multiply(rule)` inside a pricing class.
- `customer.getEmail()` passed to a report.
- `if (!user.hasRole("admin")) throw ...`
- A changed library used by the code that verifies webhook signatures.

## Counterexamples: PASS

- Code that counts stock quantities (`quantity: 42`). Inventory counts aren't money.
- A config loader whose keys are region names and debug flags.
- A template that renders SKU codes and tenant names. A tenant is an organization, not a person.
- Test files only.

## UNKNOWN if

A call site passes data through without showing what it is, for example `process(record)`,
and you can't find where `record` comes from.
