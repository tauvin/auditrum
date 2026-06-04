# API reference

This page is generated from the module docstrings. It documents the
public surface only — the names each module exports in ``__all__``,
with underscore-prefixed internals filtered out.

The stability contract for every name below lives in
[API stability](../api-stability.md): **public-stable** names change
only in backwards-compatible ways, while the **experimental** modules
(`auditrum.integrations.sqlalchemy.*` and `auditrum.observability.*`)
may change shape between minors. Read that page first if you care about
upgrade guarantees.

## Core (public-stable)

### Top-level (`auditrum`)

::: auditrum

### Schema generators (`auditrum.schema`)

::: auditrum.schema

### Trigger generation (`auditrum.triggers`)

::: auditrum.triggers

### Tracking primitives (`auditrum.tracking`)

::: auditrum.tracking.spec

::: auditrum.tracking.manager

### Executors (`auditrum.executor`)

::: auditrum.executor

### Time travel (`auditrum.timetravel`)

::: auditrum.timetravel

### Tamper evidence (`auditrum.hash_chain`)

::: auditrum.hash_chain

### Hardening (`auditrum.hardening`)

::: auditrum.hardening

### Retention (`auditrum.retention`)

::: auditrum.retention

### Revert (`auditrum.revert`)

::: auditrum.revert

### Blame (`auditrum.blame`)

::: auditrum.blame

### CLI (`auditrum.cli`)

::: auditrum.cli

### Settings (`auditrum.settings`)

::: auditrum.settings

### Context (`auditrum.context`)

::: auditrum.context

### Utilities (`auditrum.utils`)

::: auditrum.utils

## Django integration (public-stable)

### Runtime context (`auditrum.integrations.django.runtime`)

::: auditrum.integrations.django.runtime

### Middleware (`auditrum.integrations.django.middleware`)

::: auditrum.integrations.django.middleware

### Models (`auditrum.integrations.django.models`)

::: auditrum.integrations.django.models

### Admin (`auditrum.integrations.django.admin`)

::: auditrum.integrations.django.admin

### Mixins (`auditrum.integrations.django.mixins`)

::: auditrum.integrations.django.mixins

### Migration operations (`auditrum.integrations.django.operations`)

::: auditrum.integrations.django.operations

### Tracking registry (`auditrum.integrations.django.tracking`)

::: auditrum.integrations.django.tracking

### Background tasks (`auditrum.integrations.django.tasks`)

::: auditrum.integrations.django.tasks

### Template / admin helpers (`auditrum.integrations.django.utils`)

::: auditrum.integrations.django.utils

### Settings proxy (`auditrum.integrations.django.settings`)

::: auditrum.integrations.django.settings

### Executor (`auditrum.integrations.django.executor`)

::: auditrum.integrations.django.executor

### Legacy registry (`auditrum.integrations.django.audit`)

::: auditrum.integrations.django.audit

## Experimental

These modules are exported and documented, but their shape may change
between minor releases — see [API stability](../api-stability.md#public-experimental-surface).

### SQLAlchemy integration (`auditrum.integrations.sqlalchemy`)

::: auditrum.integrations.sqlalchemy.core

### Observability (`auditrum.observability`)

::: auditrum.observability.otel

::: auditrum.observability.prometheus

::: auditrum.observability.sentry
