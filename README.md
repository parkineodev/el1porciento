# El 1% - Backend FastAPI

Backend ligero para jugar con amigos al estilo del programa **“El 1%”**. Usa FastAPI, guarda preguntas en YAML y el estado de las partidas en ficheros JSON (sin base de datos).

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

## Estructura

```
el1porciento/
├─ app/
│  ├─ __init__.py
│  ├─ main.py            # FastAPI + rutas
│  ├─ models.py          # Modelos Pydantic y enums
│  ├─ question_store.py  # Carga y valida preguntas YAML
│  ├─ game_store.py      # Gestión de partidas y persistencia JSON
│  └─ data/
│     ├─ questions.yaml  # Preguntas de ejemplo
│     └─ games/          # Partidas guardadas (se crean en runtime)
├─ static/
│  └─ images/            # Imágenes para preguntas y opciones
├─ requirements.txt
└─ README.md
```

## Endpoints principales (resumen)

- `GET  /api/health` — Ping.
- `GET  /api/questions` — Lista todas las preguntas (`?include_correct=true` para ver soluciones).
- `GET  /api/questions/first` y `/api/questions/{id}/next` — Navegación por orden.
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

Los JSON de las partidas se guardan en `app/data/games/` (ignorados por Git). Las imágenes para preguntas y opciones viven en `static/images/...`.

## Datos de ejemplo

`app/data/questions.yaml` incluye varias preguntas de muestra (opción múltiple y respuesta libre). Las imágenes SVG asociadas están bajo `static/images/preguntas/` y `static/images/opciones/`.

## Notas

- No hay autenticación compleja: se usan *tokens* simples para presentador y jugadores.
- Toda la lógica de comodín, eliminación y conteo de respuestas se almacena en JSON para poder mover la carpeta a otra máquina sin perder partidas.
- Si editas el YAML de preguntas, reinicia el servidor para recargar o extiende `QuestionStore` para añadir recarga en caliente.
