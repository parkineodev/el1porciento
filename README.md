# El 1% - Backend FastAPI

Backend ligero para jugar con amigos al estilo del programa **“El 1%”**. Usa FastAPI; tanto las preguntas como el estado de las partidas se guardan en Postgres (la misma base de Supabase que usa la web de la boda), vía `DATABASE_URL`.

# Despliegue

Esta app la he desplegado en `https://el1porciento.onrender.com`con la cuenta de parkineo.dev@gmail.com y en teoría se queda muerto el container que lo corre hasta que lo visitas otra vez que despierta. Es gratis mantenerlo y cuando subes commits se actualiza.

# ¿Cómo jugar?

Se crean las preguntas en el yaml y luego:
- https://el1porciento.onrender.com/presenter para crear la sala y manejar el juego
- https://el1porciento.onrender.com/screen para enseñar a los jugadores en tiempo real las preguntas y el recuento
- https://el1porciento.onrender.com/ para unirse a una sala como jugador y jugar

## Requisitos

- Ubuntu / Debian (o similar)
- Python 3.10+
- `git`, `python3-venv`, `python3-pip`

## Instalación y arranque

```bash
git clone <repo-url> el1porciento
cd el1porciento
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Esto expone la API en `http://localhost:8000` y los recursos estáticos en `/static`.

## Frontends listos

- `http://localhost:8000/` — Pantalla de jugador (se une con código y nombre, responde, usa comodín).
- `http://localhost:8000/presenter` — Consola del presentador (crear/cargar partida, abrir/cerrar preguntas, ver resultados).
- `http://localhost:8000/screen` — Pantalla grande (muestra pregunta y conteo de vivos).
- `http://localhost:8000/admin` — Admin de preguntas (crear/editar/borrar, con subida de imágenes). Pide la contraseña de `ADMIN_PASSWORD`.

## Estructura

```
el1porciento/
├─ app/
│  ├─ __init__.py
│  ├─ main.py            # FastAPI + rutas
│  ├─ models.py          # Modelos Pydantic y enums
│  ├─ db.py               # Pool de conexión a Postgres, compartido
│  ├─ question_store.py  # Preguntas: lectura/CRUD contra Postgres
│  ├─ game_store.py      # Gestión de partidas (también en Postgres)
│  └─ data/
│     └─ games/          # Ya no se usa en producción (quedó de la versión con ficheros JSON)
├─ static/
│  ├─ admin.html          # Admin de preguntas
│  └─ images/            # Imágenes de las preguntas de ejemplo originales
├─ requirements.txt
└─ README.md
```

## Fiabilidad durante la partida

- La partida vive en memoria y se guarda en Supabase en segundo plano (cada ~0,25 s, agrupando cambios y reintentando si falla): un corte de la base de datos no para el juego. Por eso debe correr **un solo proceso** (sin `--workers`).
- Todas las acciones se pueden repetir sin efectos dobles (responder, comodín, plantarse, abrir/cerrar pregunta, siguiente...), así que los navegadores las reintentan solos (`static/net.js`).
- Se aceptan respuestas hasta 3 s después de acabar el tiempo, mientras la pregunta siga abierta.
- No subas cambios el día del evento: cada despliegue reinicia el servidor.

## Puntuación

- Preguntas normales: acertar suma los puntos de la pregunta; fallar elimina, pero el jugador conserva lo que llevaba.
- Pregunta del 1% (la que tiene «% que suele acertarla» a 1): no usa sus puntos; acertar duplica la puntuación que lleva el jugador y fallar la divide entre dos.
- Pregunta de prueba (casilla en el admin): se juega antes de empezar para enseñar cómo funciona; nadie queda eliminado, no da ni quita puntos, no admite comodín y no sale en el recuento. Conviene darle el orden más bajo (p. ej. 0) para que sea la primera.

## Variables de entorno

- `DATABASE_URL` — cadena de conexión a Postgres/Supabase (obligatoria).
- `ADMIN_PASSWORD` — contraseña compartida para `/admin` y sus endpoints
  (obligatoria para poder entrar al admin de preguntas).

## Endpoints principales (resumen)

- `GET  /api/health` — Ping. Incluye `pending_writes` (partidas con cambios aún sin guardar en Supabase), `db_error` (último error al guardar, `null` si todo va bien) e `images_cached`. Antes de jugar debe dar `pending_writes: 0` y `db_error: null`.
- `GET  /api/questions` — Lista todas las preguntas (`?include_correct=true` para ver soluciones).
- `GET  /api/questions/first` y `/api/questions/{id}/next` — Navegación por orden.
- `GET  /api/images/{id}` — Sirve una imagen subida desde el admin.
- `POST /api/admin/login` — Valida `ADMIN_PASSWORD`.
- `GET/POST/PUT/DELETE /api/admin/questions[/{id}]` — CRUD de preguntas (requiere header `X-Admin-Password`).
- `POST /api/admin/images` — Sube una imagen (multipart) y devuelve su URL (requiere header `X-Admin-Password`).
- `POST /api/games` — Crea partida (devuelve código y token de presentador).
- `GET  /api/games/{game_id}/presenter/state?presenter_token=...` — Estado para presentador.
- `GET  /api/games/{game_id}/screen/state` — Estado para la pantalla grande.
- `POST /api/games/join` — Unirse con código y nombre.
- `GET  /api/games/{game_id}/player/state?player_token=...` — Estado de un jugador.
- `POST /api/games/{game_id}/next-question` — Selecciona siguiente pregunta.
- `POST /api/games/{game_id}/open-answers` — Abre ventana de respuestas.
- `POST /api/games/{game_id}/answer` — Enviar respuesta (opción o texto).
- `POST /api/games/{game_id}/joker` — Usar comodín (una sola vez).
- `POST /api/games/{game_id}/close-answers` — Cierra respuestas y calcula resultados.
- `GET  /api/games/{game_id}/questions/{question_id}/results` — Resultados de una pregunta.
- `POST /api/games/{game_id}/finish` — Marca partida como terminada.

Las partidas se guardan en la tabla `elporciento_games` de Postgres. Las preguntas viven en `elporciento_questions` (y sus imágenes subidas, en `elporciento_question_images`); ambas tablas las crea una migración del repo de la wedding-app, no esta app. Las imágenes originales de las preguntas de ejemplo siguen sirviéndose desde `static/images/...`.

## Notas

- Autenticación mínima: tokens simples para presentador y jugadores, y una contraseña compartida (`ADMIN_PASSWORD`) para el admin de preguntas — nada de sesiones ni JWT.
- Editar preguntas desde `/admin` se aplica al momento (no hace falta reiniciar el servidor): cada creación/edición/borrado llama a `QuestionStore.reload()`.
