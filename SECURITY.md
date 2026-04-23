# Security policy

auditrum is an audit and compliance library. Vulnerabilities in it
can leak attacker-controlled data into a supposedly-tamper-evident
log, let a compromised app role forge audit rows, or corrupt the
chain integrity guarantee. We take reports seriously and respond
quickly.

## Reporting a vulnerability

**Do not open a public GitHub issue.** Email the maintainer directly:

**andrey.karbanovich@icloud.com**

Include, at minimum:

* A description of the vulnerability.
* Affected version(s). If you can confirm the bug is present on the
  latest ``main`` branch, that saves us a step.
* A minimal reproducer — ideally a failing test case or a short
  Python / SQL snippet. Don't include real production data; scrub
  identifiers.
* Your assessment of severity (low / medium / high / critical) and
  any CVSS components you can estimate.

If encrypted email is necessary, ask in plaintext first and we'll
set up a PGP handshake.

## Response SLA

The maintainer is currently one person working part-time. Reports
are triaged on the following targets:

| Severity       | First response | Fix shipped to ``main`` |
|----------------|----------------|-------------------------|
| **critical**   | within 72 h    | within 1 week           |
| **high**       | within 1 week  | within 2 weeks          |
| **medium**     | within 2 weeks | within 1 month          |
| **low**        | within 1 month | next scheduled release  |

"Critical" means: remote code execution, arbitrary SQL injection
through a documented API, hash-chain forgery, credentials disclosure,
or any path that makes the "append-only" claim in
``docs/hardening.md`` untrue.

"High" means: any exploit by an attacker who already has normal app
role access (row forgery, privilege escalation within the DB,
denial-of-service of the audit subsystem), plus PII disclosure
through logs/metrics that shouldn't carry it.

If you hit the response SLA without a reply, re-send with "URGENT"
in the subject line. If you still don't get a reply within another
24 h, you're free to disclose publicly — but please don't; reach
out on GitHub (open a non-detailed issue mentioning "security") so
we have a chance to notice the stalled thread.

## What to expect once a report is in flight

1. **Acknowledgement** within the SLA window — a reply confirming
   the report was received and whether we can reproduce it.
2. **Fix development** on a private branch. If the issue needs a
   coordinated advisory across users, we request a disclosure window
   agreed with the reporter.
3. **Release** bundling the fix into the next available point release
   (``x.y.z+1``). Security releases get a dedicated row in
   ``CHANGELOG.md`` and a corresponding GitHub Security Advisory.
4. **Credit** to the reporter in the advisory, CHANGELOG entry, and
   any write-up — unless you prefer to stay anonymous.

## Scope

Vulnerabilities **in scope**:

* ``auditrum/`` core and all shipped integrations under
  ``auditrum/integrations/``.
* Generated PL/pgSQL — if the trigger body has an injection or
  privilege-confusion bug, that's in scope.
* The ``auditrum`` CLI.
* The Django admin surface we ship (``AuditLogAdmin``,
  ``AuditContextAdmin``, ``AuditHistoryMixin``).
* Build/release workflows (``.github/workflows/*.yml``) — if a
  contributor can poison a published artifact via a workflow
  vulnerability, that's in scope.

**Out of scope**:

* Vulnerabilities in upstream dependencies (psycopg, Django,
  SQLAlchemy, OpenTelemetry, Sentry). Report those to their
  respective maintainers; we'll pin updated versions once available.
* User-application-level bugs (e.g. your app stores raw credentials
  in ``audit_context.metadata``). Those are deployment mistakes.
  That said, if you think our API makes them too easy, we want to
  hear it — open a regular issue.
* Performance issues without a security angle — open a GitHub
  issue instead.

## Supported versions

At any given time, only the latest **minor** release receives
security fixes. Older versions are expected to upgrade. Once a
stable ``1.0`` ships, we intend to maintain the most recent ``1.x``
line for security fixes for **at least 12 months** after a new
minor — the exact window gets nailed down in ``ROADMAP.md`` before
the ``1.0`` announcement.

| Version range | Security support                                              |
|---------------|---------------------------------------------------------------|
| ``0.x``       | Latest minor only. Upgrade path always goes forward.          |
| ``1.x``       | Policy TBD pre-1.0; see ROADMAP "Open questions".              |

## Known-historical issues

The 0.3.1 release notes in ``CHANGELOG.md`` document 21 findings
from the pre-release three-pass review (5 critical, 6 high, 10
medium). All were fixed in that release. Use them as a reference
for the shape of issues we expect to see.
