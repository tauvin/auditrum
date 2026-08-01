"""Record :class:`AuditLog` / :class:`AuditContext` in Django's migration state.

Every migration auditrum shipped before this one is pure ``RunSQL`` /
``RunPython``: the tables and PL/pgSQL helpers are created by raw DDL
because they are partitioned and trigger-driven in ways Django's schema
editor cannot express. That works for ``migrate`` — but it leaves the
*model state* empty. Django's autodetector compares the models it can
import against the state the migrations build up, finds two models it has
never seen, and reports them as unmigrated changes forever::

    $ python manage.py makemigrations --check
    Migrations for 'auditrum_django':
      + Create model AuditContext
      + Create model AuditLog

Downstream projects hit this on every ``makemigrations`` run and on any CI
step that gates on ``makemigrations --check`` — auditrum looked
permanently out of date through no fault of theirs.

This migration closes the gap by declaring the two models to the state
graph. It emits **no DDL**: both models are ``managed = False``, and
``CreateModel.database_forwards`` skips unmanaged models entirely
(``Operation.allow_migrate_model`` returns ``False`` when
``Model._meta.can_migrate()`` is false). Applying it on an existing
deployment therefore touches nothing — the real tables stay exactly as
``0001_initial`` built them — while ``makemigrations`` goes quiet.

``db_table`` is read from :data:`audit_settings` at import time, the same
way ``0001_initial`` reads it, rather than being frozen to the default.
A project running ``PGAUDIT_TABLE_NAME = "my_auditlog"`` gets state that
matches its own models; freezing the literal ``"auditlog"`` here would
just trade the CreateModel noise for a permanent ``AlterModelTable``.
Because no DDL is emitted, state that varies by settings is safe.
"""

import uuid

from django.db import migrations, models

from auditrum.integrations.django.settings import audit_settings


class Migration(migrations.Migration):
    dependencies = [
        ("auditrum_django", "0004_diff_gin_index_optional"),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditContext",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("metadata", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Audit Context",
                "verbose_name_plural": "Audit Contexts",
                "db_table": audit_settings.context_table_name,
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="AuditLog",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("operation", models.CharField(max_length=16)),
                ("changed_at", models.DateTimeField(auto_now_add=True)),
                ("object_id", models.CharField(max_length=255)),
                ("table_name", models.CharField(max_length=255)),
                ("user_id", models.IntegerField(blank=True, null=True)),
                ("old_data", models.JSONField(blank=True, null=True)),
                ("new_data", models.JSONField(blank=True, null=True)),
                ("diff", models.JSONField(blank=True, null=True)),
                ("meta", models.JSONField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "Audit Log",
                "verbose_name_plural": "Audit Logs",
                "db_table": audit_settings.table_name,
                "managed": False,
            },
        ),
    ]
