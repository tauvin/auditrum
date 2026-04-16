import atexit
import contextlib

from .utils import audit_tracked

# Bind the context manager to a module-level name so CPython's
# refcount-driven GC doesn't immediately reclaim it — when the
# manager goes out of scope its generator is closed, which triggers
# __exit__, which pops the audit context and resets "source". Holding
# a reference here keeps the shell session's source="shell" stamp on
# every audit row written from the REPL.
_shell_ctx = audit_tracked(source="shell")
_shell_ctx.__enter__()


@atexit.register
def _release_shell_ctx() -> None:
    # Run cleanup before interpreter shutdown so the generator's
    # finally block runs while its transitive imports (the executor
    # chain in auditrum.context) are still importable. Swallow any
    # late shutdown races — the process is going away either way.
    with contextlib.suppress(Exception):
        _shell_ctx.__exit__(None, None, None)
