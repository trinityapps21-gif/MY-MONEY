"""Import mode: while active, bulk-imported historical records do not send
notifications, post feed events or require cash on hand for old disbursements."""
from contextlib import contextmanager
from contextvars import ContextVar

_active = ContextVar("import_active", default=False)


def active() -> bool:
    return _active.get()


@contextmanager
def importing():
    token = _active.set(True)
    try:
        yield
    finally:
        _active.reset(token)
