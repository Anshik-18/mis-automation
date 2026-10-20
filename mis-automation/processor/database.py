"""PostgreSQL access. One short-lived connection per unit of work (psycopg 3); a pool is used when available."""
from __future__ import annotations

import contextlib
from typing import Iterator

import psycopg

try:                                        # optional: service uses a pool, tests/CLI use plain connections
    from psycopg_pool import ConnectionPool
except Exception:                           # pragma: no cover
    ConnectionPool = None


SEARCH_PATH_OPT = "-c search_path=mis,public -c timezone=UTC"


def connect(url: str, **kw):
    return psycopg.connect(url, options=SEARCH_PATH_OPT, **kw)


class Database:
    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 5, use_pool: bool = True):
        self.url = url
        self._pool = None
        if use_pool and ConnectionPool is not None:
            self._pool = ConnectionPool(url, min_size=min_size, max_size=max_size, open=True,
                                        kwargs={"autocommit": False, "options": SEARCH_PATH_OPT}, timeout=30)

    @contextlib.contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        """Yields a connection; commits on success, rolls back on error."""
        if self._pool is not None:
            with self._pool.connection() as conn:
                yield conn
            return
        conn = connect(self.url)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
