"""Session advisory locks order external operations across token-refresh commits."""
from contextlib import contextmanager
from threading import Lock

from sqlalchemy import text

_locks = [Lock() for _ in range(256)]


@contextmanager
def operation_lock(engine, resource_id: int, namespace: int = 220260502):
    if engine.dialect.name == "postgresql":
        with engine.connect() as connection:
            params = {"namespace": namespace, "key": resource_id % 2147483647}
            acquired = connection.execute(text("SELECT pg_try_advisory_lock(:namespace, :key)"), params).scalar_one()
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute(text("SELECT pg_advisory_unlock(:namespace, :key)"), params)
                    connection.commit()
    else:
        lock = _locks[(namespace + resource_id) % len(_locks)]
        acquired = lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()
