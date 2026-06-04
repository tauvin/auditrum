# Case study — Bidwise

**Status:** numbers below were measured on bidwise's **production**
database in June 2026 (read-only introspection plus
``EXPLAIN (ANALYZE) … ; ROLLBACK`` for the isolated trigger cost). They
back the "Cross-check: bidwise" section of
[performance.md](../performance.md).

Bidwise is a Django / PostgreSQL 17.5 auction-bidding platform — a
sister project to [catalog](catalog.md) at the same company. auditrum has
been live there for a shorter time than catalog, but bidwise carries
real user traffic, so it supplies the one thing catalog's pre-prod
sample can't: **production throughput**.

## Why bidwise matters as a second deployment

Two independent deployments on different schemas turn a single data
point into a trend. Where they **agree**, the conclusion is robust;
where they **differ**, the difference is explained by the workload.
Bidwise specifically contributes:

* **Real production throughput** — sustained bursts the pre-prod catalog
  sample never reaches.
* **Much deeper histories** — invoices that churn for their whole
  lifecycle.
* A **second, independent confirmation** of the GIN-on-diff finding.
* A real-world **incomplete-migration** lesson (below).

## Workload shape

Measured over a 15-day window (2026-05-20 → 06-04):

* **Audit log size:** **3.84M events** in a monthly RANGE-partitioned
  ``auditlog``.
* **Tracked models:** eight (``provider_*`` and ``users_*``).
* **Hot table:** ``provider_lot`` produces **2.44M events**, of which
  **98% are UPDATEs** (lot state changes throughout an auction);
  ``provider_sale`` 1.17M, ``provider_invoice`` 142k, the rest small.
* **Peak write rate:** **~49 events/s** (2,943 in the busiest minute),
  ~2.9/s average — real production traffic, an order of magnitude above
  catalog's pre-prod peak (~3.7/s).
* **Hash chain:** disabled.

## What we measured

(Methodology and the reproducible reference microbenchmark live in
[performance.md](../performance.md).)

* **Trigger overhead (per UPDATE, isolated):** ``provider_lot`` ~**1.38
  ms**, ``provider_invoice`` ~**1.38 ms** — higher than catalog's
  ~0.3–0.5 ms because the ``provider_*`` rows are wider (bigger jsonb
  diffs). This is the headline cross-deployment lesson: trigger overhead
  scales with **row width**, not as a fixed constant.
* **Time-travel latency:** the deepest-history row in the whole
  deployment (a ``provider_sale`` with **2,207 revisions**) reconstructs
  its index scan in **~11.3 ms** — 4× catalog's deepest row for only 2.3×
  the time, so the composite index scales **sub-linearly** with depth.
* **History depth:** much deeper than catalog. ``provider_invoice`` rows
  carry a **median of 119 revisions** (p99 = 840, max = 1,535) — invoices
  are updated continuously through their lifecycle.
* **Storage footprint:** **10 GB total** for 3.84M events — ~**2.9 KB per
  event** (narrower than catalog's 3.7 KB).
* **GIN-on-diff, again unused:** **0 scans**, ~**859 MB on one
  partition**. A second independent deployment with the same result was
  the decisive evidence for making the index opt-in
  ([#6](https://github.com/tauvin/auditrum/issues/6)).

## The incomplete-migration lesson

Bidwise had adopted auditrum *on top of* a home-grown audit trigger from
before auditrum existed — and the old triggers were never removed. Every
tracked table carried **two** audit triggers: auditrum's
(``audit_<table>_trigger``, writing to ``auditlog``) and the legacy one
(``<table>_audit_trigger``, writing to a separate ``common_auditlog``
table with the old hardcoded-``content_type_id`` design).

The cost was invisible until measured: an ``EXPLAIN (ANALYZE)`` showed
**two** ``Trigger`` lines, ~1.6–2 ms for the legacy one on top of
auditrum's ~1.38 ms — **doubling** the per-write audit overhead, and
filling a redundant table. Crucially, the legacy trigger wrote to a
*different* table, so auditrum's ``auditlog`` figures were never
double-counted.

The fix: drop the nine legacy triggers and their functions via a Django
migration, keeping ``common_auditlog`` as a frozen archive of
pre-auditrum history. The full recipe is in the
[migration cookbook](../migration-cookbook.md#recipe-5-removing-a-legacy-in-house-audit-trigger).

**Takeaway:** when adopting auditrum alongside an existing audit
mechanism, audit your triggers (``EXPLAIN ANALYZE`` shows each one by
name) and remove the predecessor — otherwise you pay for both.

## Catalog vs bidwise at a glance

| Metric | Catalog (pre-prod) | Bidwise (production) |
|--------|--------------------|----------------------|
| Span / events | 37 d / 7.2M | 15 d / 3.84M |
| Peak rate | ~3.7/s | **~49/s** |
| Footprint | 25 GB (~3.7 KB/event) | 10 GB (~2.9 KB/event) |
| Trigger overhead (UPDATE) | ~0.3–0.5 ms | ~1.38 ms |
| Deepest history | 570 rev → 4.9 ms | 2,207 rev → 11.3 ms |
| GIN-on-diff | 0 scans, 704 MB/part. | 0 scans, 859 MB/part. |

The agreements (overhead tracks row width; time-travel scales
sub-linearly; the diff GIN is dead weight) are the robust,
two-deployment conclusions auditrum 1.0 leans on.
