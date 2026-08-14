# Security policy

## Reporting a vulnerability

Please report security issues **privately** — open a [GitHub security advisory][advisory]
rather than a public issue, so a fix can ship before the details are public.

Include what you were able to do, the steps to reproduce it, and the version or commit.

[advisory]: https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability

## What this platform assumes

It is built to run on a private, segmented network — in many deployments, one with no route
to the Internet at all. Several defaults are deliberately permissive *because of that
assumption*, and they are wrong for a platform exposed to a hostile network:

* Private address ranges are allowed for node agent URLs, and a source-IP mismatch during
  enrolment is recorded rather than refused (`app/core/agent_url.py`).
* `/metrics` is unauthenticated. It is not proxied through nginx, so the network is the
  boundary — if you expose it, that assumption no longer holds.
* The platform publishes plain HTTP and expects TLS to be terminated in front of it.

If you deploy it differently, review those first.

## Secrets

`SECURITY__ENCRYPTION_KEY` decrypts every stored credential: node agent tokens, tool
credentials, chat keys. Two properties follow, both intentional:

* **No backup contains it.** A restore without the matching key is refused rather than
  producing a platform full of undecryptable rows.
* **Rotating it invalidates everything already encrypted.**

`make up` generates working secrets into `.env` on first run so a developer is running in
one command. That is a development convenience. Production secrets belong wherever your
organisation keeps secrets, and `.env` is git-ignored precisely so they never reach a
commit.

## What is enforced in the codebase

* **Air-gap discipline** — `make airgap` (part of `make lint`) fails the build if a runtime
  code path shells out to a network fetcher, if a dependency is unpinned, or if a container
  image is not digest-pinned.
* **Authorisation** — every mutating route declares a permission; the layering contracts in
  `make lint` fail if a router touches a repository or the database directly.
* **Agents cannot execute shell commands.** `COMMAND` and `PYTHON` tool types are
  registerable but disabled: the specification forbids unrestricted execution by agents,
  and the calculator parses expressions to an AST against a whitelist rather than calling
  `eval`.

## Supported versions

This is pre-1.0. Fixes land on the default branch; there are no maintained release
branches yet.
