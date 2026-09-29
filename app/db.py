from __future__ import annotations

import os
from urllib.parse import unquote, urlparse

from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool


def _build_conninfo(database_url: str) -> str:
    """Convierte la URL de conexión en un conninfo con el puerto ya como
    entero de Python. Si se deja el puerto dentro de la URL (p. ej.
    ":6543"), en contenedores mínimos como los de Render (sin /etc/services
    completo) getaddrinfo() puede intentar resolverlo como nombre de
    servicio en vez de número y fallar con "Servname not supported for
    ai_socktype" -- pasarlo ya parseado como int evita esa ruta de código.
    """
    parsed = urlparse(database_url)

    return make_conninfo(
        dbname=(parsed.path or "/postgres").lstrip("/") or "postgres",
        host=parsed.hostname,
        password=unquote(parsed.password) if parsed.password else None,
        port=parsed.port or 5432,
        user=unquote(parsed.username) if parsed.username else None,
    )


_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Pool de conexión compartido por GameStore y QuestionStore -- ambos
    hablan con las mismas tablas de la misma base, no tiene sentido que cada
    uno abra su propio pool.
    """
    global _pool
    if _pool is None:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL no está configurada (cadena de conexión a Postgres/Supabase)"
            )
        _pool = ConnectionPool(
            _build_conninfo(database_url), min_size=1, max_size=5, kwargs={"autocommit": True}
        )
    return _pool
