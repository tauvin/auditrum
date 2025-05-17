<file name=0 path=README.md>#auditrum

A PostgreSQL audit system for tracking database changes with rich contextual information, featuring seamless Django integration.

---

## ✨ Features

- **Automatic Change Tracking** – PostgreSQL triggers to log all `INSERT`, `UPDATE`, and `DELETE`
- **Rich Context** – Who, when, why, and from where
- **Partitioned Storage** – Time-partitioned audit table
- **Django Integration** – Models, admin, middleware
- **Flexible Configuration** – Track/include/exclude fields, add conditions
- **Performance Optimized** – Minimal database overhead

---

## 📦 Installation

### With `pip`

```bash
pip install auditrum
```

To include Django support:

```bash
pip install auditrum[django]
```

---

### With [`uv`](https://github.com/astral-sh/uv)

```bash
uv add auditrum
uv add auditrum[django]
```

---

## 🚀 Quick Start

### 🔧 With Django

1. **Add to `INSTALLED_APPS`:**

```python
INSTALLED_APPS = [
    # ...
    'auditrum.integrations.django',
]
```

2. **Add middleware:**

```python
MIDDLEWARE = [
    # ...
    'auditrum.integrations.django.middleware.RequestIDMiddleware',
    'auditrum.integrations.django.middleware.AuditrumMiddleware',
]
```

3. **Register models:**

Create a new `audit.py` file inside your Django app (e.g., `yourapp/audit.py`) and register models there:

```python
# yourapp/audit.py

from auditrum.integrations.django.audit import register
from .models import User

register(User, track_only=["name", "email"])
```

The `auditrum` integration will automatically discover and execute this file (like `admin.py`), so no need to import it manually.

4. **Run migrations:**

```bash
python manage.py migrate
```

This will create the audit table and set up triggers automatically.

5. **Set up monthly partitions:**

Add a cron job to run the following command on the last day of each month. This will ensure partitions exist for upcoming months:

```bash
python manage.py audit_add_partitions --months 3
```

This will create audit table partitions for the next 3 months.

#### ⏰ Cron Job Example

To automatically run the partition creation every month (e.g. on the 28th at 23:50), add the following cron entry:

```cron
50 23 28 * * /path/to/your/venv/bin/python /path/to/your/project/manage.py audit_add_partitions --months 3
```

- Replace `/path/to/your/venv/bin/python` with the full path to your virtualenv's Python interpreter.
- Replace `/path/to/your/project/manage.py` with the path to your project's manage.py file.
- Adjust the `--months 3` argument as needed to define how far ahead partitions should be created.

6. **Enable audit context for management commands (optional)**

To track changes made via `manage.py` commands (e.g. `migrate`, `shell`) with proper `source` and `change_reason` in audit logs, you can modify your `manage.py` like this:

```python
# manage.py

from auditrum.utils import audit_tracked


def main():
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "your_app.settings")

    AUDIT_COMMAND_SOURCES = {
        "shell": "shell",
        "migrate": "migration",
        "makemigrations": "migration",
    }

    cmd = sys.argv[1] if len(sys.argv) > 1 else None
    audit_source = AUDIT_COMMAND_SOURCES.get(cmd)

    if audit_source:
        try:
            audit_tracked(source=audit_source).__enter__()
        except Exception as e:
            print(f"[audit] failed to enable tracking for source='{audit_source}': {e}")

    if cmd == "shell":
        try:
            import apps.audit.shell_context  # noqa
        except ImportError:
            print("[audit] Warning: shell_context not found. Skipping shell audit.")

    ...
```

This ensures your migrations and shell activity are properly attributed in the audit log.

---

### 🧩 Without Django

```python
from auditrum.schema import generate_auditlog_table_sql
from auditrum.triggers import generate_trigger_sql
import psycopg

with psycopg.connect("your_connection_string") as conn, conn.cursor() as cursor:
    # Create audit table
    cursor.execute(generate_auditlog_table_sql("auditlog"))

    # Create trigger for a table
    sql = generate_trigger_sql(
        table_name="users",
        track_only=["name", "email"]
    )
    cursor.execute(sql)
    conn.commit()
```

---

## ⚙️ Configuration Examples

### ✅ Track only specific fields

```python
register(User, track_only=["name", "email"])
```

### ❌ Exclude specific fields

```python
register(Product, exclude_fields=["created_at", "updated_at"])
```

### 📐 Add conditions

```python
register(Subscription, log_conditions="NEW.is_active = TRUE")
```

---

## 📚 Adding Context to Changes

### Using decorator:

```python
from auditrum.context import with_change_reason


@with_change_reason("User requested password reset")
def reset_password(user_id):
    ...
```

### Using context manager:

```python
from auditrum.context import audit_context

with audit_context.use_change_reason("Bulk update for compliance"):
    ...
```

---

## 📖 Documentation

👉 Full docs at: [https://auditrum.readthedocs.io/](https://auditrum.readthedocs.io/)

---

## 🪪 License

This project is licensed under the MIT License. See the `LICENSE` file for details.
</file>
