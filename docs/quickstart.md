# Quickstart

From `pip install` to your first audit row in about five minutes, on
Django. For other frameworks (SQLAlchemy, raw psycopg), install options,
and the details behind each step, see [Getting started](getting-started.md).

## 1. Install

```bash
pip install "auditrum[django]"
```

## 2. Wire it up

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "django.contrib.contenttypes",
    "auditrum.integrations.django",
]

MIDDLEWARE = [
    # ...
    "auditrum.integrations.django.middleware.AuditrumMiddleware",
]
```

The middleware propagates per-request context (user, IP, URL, request
id) into every query, so the audit trigger records *who* and *from
where* with no app-level plumbing.

## 3. Track a model

```python
# myapp/models.py
from django.db import models
from auditrum.integrations.django import track, AuditedModelMixin


@track(fields=["status", "total"])
class Order(AuditedModelMixin, models.Model):
    status = models.CharField(max_length=32)
    total = models.DecimalField(max_digits=10, decimal_places=2)
```

## 4. Install the schema and triggers

```bash
python manage.py migrate auditrum_django   # audit log, context table, partitions, helpers
python manage.py auditrum_makemigrations   # one migration per @track'd model
python manage.py migrate                   # installs the trigger on `Order`
```

## 5. Make a change

```python
# manage.py shell
from myapp.models import Order

o = Order.objects.create(status="new", total=99)
o.status = "paid"
o.save()
```

## 6. See the audit trail

Query it from the ORM:

```python
for event in o.audit_events.order_by("changed_at"):
    print(event.operation, event.diff)
# INSERT {'status': {'old': None, 'new': 'new'}, 'total': {'old': None, 'new': '99'}}
# UPDATE {'status': {'old': 'new', 'new': 'paid'}}
```

Every change is captured as a paired `{field: {old, new}}` diff — no app
code wrote those rows; the database trigger did.

Or look at it visually: register `Order` in the Django admin and open any
order's **History** tab — auditrum renders each change as an
`old → new` diff, with the source/user from the request context.

## Next steps

* [Django integration](django.md) — ORM helpers, middleware options,
  admin, Celery/background tasks.
* [Time travel](time-travel.md) — reconstruct any row's state at a past
  timestamp.
* [Production deployment](deployment.md) — roles, retention, monitoring,
  backup, rollback.
