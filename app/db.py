from __future__ import annotations

import logging
import os
import time
from typing import Callable, TypeVar
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

log = logging.getLogger("el1porciento.db")

T = TypeVar("T")


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
        # prepare_threshold=None: el pooler de Supabase (modo transacción,
        # puerto 6543) no soporta sentencias preparadas en el servidor, y
        # psycopg las crea sola tras 5 usos de la misma consulta -- a partir
        # de ahí esas consultas fallaban de forma intermitente (500).
        #
        # check: comprueba cada conexión antes de prestarla -- el pooler de
        # Supabase puede cerrar las que llevan un rato quietas, y así nunca
        # se usa una conexión muerta.
        # timeout: si la base no responde, se falla a los 10 s (y se
        # reintenta) en vez de dejar la petición colgada 30 s.
        _pool = ConnectionPool(
            _build_conninfo(database_url),
            min_size=2,
            max_size=5,
            timeout=10,
            max_idle=120,
            check=ConnectionPool.check_connection,
            kwargs={
                "autocommit": True,
                "prepare_threshold": None,
                "connect_timeout": 5,
            },
            open=True,
        )
    return _pool


def with_retry(fn: Callable[[], T], *, attempts: int = 4, what: str = "consulta") -> T:
    """Ejecuta `fn` reintentando ante fallos de red/base de datos (cortes
    puntuales del pooler de Supabase), con esperas crecientes. Solo para
    operaciones idempotentes: lecturas o upserts completos.
    """
    delay = 0.2
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- cualquier fallo de la BD se reintenta igual
            if attempt == attempts:
                raise
            log.warning("%s falló (intento %d/%d): %s", what, attempt, attempts, exc)
            time.sleep(delay)
            delay = min(delay * 2, 2.0)
    raise AssertionError("inalcanzable")
