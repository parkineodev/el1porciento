from __future__ import annotations

import logging
import secrets
import string
import threading
import time
from typing import Dict, List, Optional, Set, Tuple

from fastapi import HTTPException

from .db import get_pool, with_retry
from .models import (
    AnswerRecord,
    GamePhase,
    GameSession,
    Player,
    PlayerStatus,
    Question,
    QuestionResult,
    QuestionType,
    RosterEntry,
    ScoreSnapshot,
)

log = logging.getLogger("el1porciento.games")

# Margen tras acabar el tiempo en el que aún se acepta una respuesta: el móvil
# la envía a tiempo pero tarda en llegar (red del móvil, reintentos). El
# presentador sigue siendo quien cierra la pregunta.
ANSWER_GRACE_SECONDS = 3.0

# Cada cuánto como mínimo se vuelca a la base de datos una partida con
# cambios: todas las respuestas que llegan en ese intervalo van en una sola
# escritura.
FLUSH_INTERVAL_SECONDS = 0.25

# Cuánto se recuerda que un id de partida no existe, para que un móvil con una
# partida vieja guardada no consulte la base de datos en cada sondeo.
MISSING_TTL_SECONDS = 10.0


def _generate_id(prefix: str) -> str:
    suffix = secrets.token_hex(4)
    return f"{prefix}_{suffix}"


def _generate_code(length: int = 4) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _generate_token() -> str:
    return secrets.token_urlsafe(24)


def score_after_answer(score: float, question: Question, correct: bool) -> float:
    """Puntuación del jugador tras corregir su respuesta.

    - Pregunta del 1%: acertar duplica lo que lleva; fallar lo divide entre dos.
    - Resto: acertar suma los puntos de la pregunta; fallar lo deja como está
      (queda eliminado, pero conserva lo que llevaba).
    - Pregunta de prueba: no cambia nada.
    """
    if question.practice:
        return score
    if question.is_one_percent:
        return score * 2 if correct else max(0, score * 0.5)
    return score + question.points if correct else score


def _normalize_free_text(value: Optional[str]) -> str:
    return (value or "").strip().lower()


class GameStore:
    """Gestiona partidas persistidas en Postgres (tabla elporciento_games).

    Cada partida se guarda como un único blob jsonb, igual que antes se
    guardaba como un único fichero JSON — mismo modelo de datos, solo cambia
    dónde vive. La fuente de verdad durante la partida es la caché en memoria
    (self._games); requiere que el proceso corra en una sola instancia (sin
    --workers ni autoscaling).

    Las escrituras a la base de datos van en segundo plano: cada cambio se
    aplica en memoria y se contesta al momento, y un hilo aparte vuelca la
    partida (agrupando todos los cambios de los últimos
    FLUSH_INTERVAL_SECONDS) reintentando hasta que la base responda. Así 60
    respuestas a la vez no hacen cola detrás de 60 escrituras, y un corte
    momentáneo de Supabase no se nota en los móviles.
    """

    def __init__(self, database_url: Optional[str] = None):
        # `database_url` se mantiene como parámetro por compatibilidad, pero
        # ya no se usa para abrir un pool propio -- GameStore y
        # QuestionStore comparten el mismo pool (ver app/db.py).
        del database_url
        self._pool = get_pool()
        self._games: Dict[str, GameSession] = {}
        self._code_index: Dict[str, str] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._missing: Dict[str, float] = {}

        self._dirty: Set[str] = set()
        self._deleted: Set[str] = set()
        self._dirty_cond = threading.Condition()
        # Solo un volcado a la vez, para que una foto vieja de la partida no
        # pueda llegar a la base después de una más nueva.
        self._flush_lock = threading.Lock()
        self.last_flush_ok_at: Optional[float] = None
        self.last_flush_error: Optional[str] = None
        threading.Thread(target=self._flush_loop, name="game-flusher", daemon=True).start()

    def _lock_for(self, game_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(game_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[game_id] = lock
            return lock

    def _save(self, game: GameSession) -> None:
        """Marca la partida para volcarla a la base en segundo plano. Se
        llama con el lock de la partida cogido, tras mutarla en memoria."""
        self._games[game.id] = game
        self._code_index[game.code.upper()] = game.id
        with self._dirty_cond:
            self._dirty.add(game.id)
            self._dirty_cond.notify()

    def _write_row(self, game_id: str, code: str, payload: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                insert into public.elporciento_games (id, code, data, updated_at)
                values (%s, %s, %s::jsonb, now())
                on conflict (id) do update
                set code = excluded.code, data = excluded.data, updated_at = now()
                """,
                (game_id, code, payload),
            )

    def _snapshot(self, game_id: str) -> Optional[Tuple[str, str]]:
        game = self._games.get(game_id)
        if game is None:
            return None
        with self._lock_for(game_id):
            if game_id in self._deleted:
                return None
            # model_dump_json (serializador en Rust de pydantic) es mucho más
            # rápido que jsonable_encoder, y se hace con el lock cogido.
            return game.code.upper(), game.model_dump_json()

    def flush(self) -> bool:
        """Vuelca a la base todas las partidas con cambios pendientes.
        Devuelve False si alguna no se pudo escribir (queda pendiente)."""
        with self._flush_lock:
            with self._dirty_cond:
                pending = list(self._dirty)
                self._dirty.clear()
            ok = True
            for game_id in pending:
                snapshot = self._snapshot(game_id)
                if snapshot is None:
                    continue
                try:
                    self._write_row(game_id, *snapshot)
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    self.last_flush_error = type(exc).__name__
                    log.warning("No se pudo guardar la partida %s, se reintentará: %s", game_id, exc)
                    with self._dirty_cond:
                        self._dirty.add(game_id)
            if ok:
                self.last_flush_ok_at = time.time()
                self.last_flush_error = None
            return ok

    def _flush_loop(self) -> None:
        backoff = 0.5
        while True:
            with self._dirty_cond:
                while not self._dirty:
                    self._dirty_cond.wait()
            time.sleep(FLUSH_INTERVAL_SECONDS)
            try:
                ok = self.flush()
            except Exception:  # noqa: BLE001 -- el hilo no puede morir nunca
                log.exception("Error inesperado volcando partidas")
                ok = False
            if ok:
                backoff = 0.5
            else:
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

    def flush_before_exit(self, timeout: float = 20.0) -> None:
        """Al apagar el proceso (redeploy/reinicio en Render): vuelca lo que
        quede pendiente, reintentando hasta `timeout` segundos."""
        deadline = time.time() + timeout
        while self.pending_writes() and time.time() < deadline:
            if not self.flush():
                time.sleep(0.5)

    def pending_writes(self) -> int:
        with self._dirty_cond:
            return len(self._dirty)

    @staticmethod
    def _hydrate(raw: dict) -> GameSession:
        # Compat: migrar cashout_open legacy a keep/boost si no existen
        if "cashout_open" in raw:
            raw.setdefault("cashout_keep_open", raw.get("cashout_open", False))
            raw.setdefault("cashout_boost_open", raw.get("cashout_open", False))
        return GameSession(**raw)

    def _adopt_or_cache(self, game: GameSession) -> GameSession:
        # Si dos peticiones cargan la misma partida desde la BD a la vez
        # (solo puede pasar justo tras un reinicio del proceso), esto
        # asegura que ambas terminen compartiendo el mismo objeto en memoria
        # en vez de mutar copias separadas que se pisarían al guardar.
        with self._locks_guard:
            existing = self._games.get(game.id)
            if existing is not None:
                return existing
            self._games[game.id] = game
            self._code_index[game.code.upper()] = game.id
            return game

    def _fetch_one(self, sql: str, params: tuple):
        def run():
            with self._pool.connection() as conn:
                return conn.execute(sql, params).fetchone()

        try:
            return with_retry(run, what="leer partida")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503, detail="Base de datos no disponible, reintenta"
            ) from exc

    def _load_from_db(self, game_id: str) -> Optional[GameSession]:
        missing_at = self._missing.get(game_id)
        if missing_at and time.time() - missing_at < MISSING_TTL_SECONDS:
            return None
        row = self._fetch_one(
            "select data from public.elporciento_games where id = %s", (game_id,)
        )
        if not row:
            self._missing[game_id] = time.time()
            return None
        return self._adopt_or_cache(self._hydrate(row[0]))

    def _load_by_code_from_db(self, code: str) -> Optional[GameSession]:
        row = self._fetch_one(
            "select data from public.elporciento_games where code = %s", (code.upper(),)
        )
        if not row:
            return None
        return self._adopt_or_cache(self._hydrate(row[0]))

    def create_game(
        self, presenter_name: str, roster: Optional[List[RosterEntry]] = None
    ) -> GameSession:
        game_id = _generate_id("game")
        code = _generate_code()
        presenter_id = _generate_id("host")
        presenter_token = _generate_token()

        game = GameSession(
            id=game_id,
            code=code,
            presenter_id=presenter_id,
            presenter_name=presenter_name,
            presenter_token=presenter_token,
            roster=roster or [],
        )
        # La partida nueva se escribe ya (no en segundo plano): quien la crea
        # (la web de la boda) guarda su id y cuenta con que exista.
        try:
            with_retry(
                lambda: self._write_row(game.id, game.code.upper(), game.model_dump_json()),
                what="crear partida",
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503, detail="Base de datos no disponible, reintenta"
            ) from exc
        self._games[game.id] = game
        self._code_index[game.code.upper()] = game.id
        return game

    def get_roster_by_code(self, code: str) -> List[RosterEntry]:
        game = self.get_game_by_code(code)
        return game.roster

    def get_game(self, game_id: str) -> GameSession:
        game = self._games.get(game_id) or self._load_from_db(game_id)
        if not game:
            raise HTTPException(status_code=404, detail="Partida no encontrada")
        return game

    def get_game_by_code(self, code: str) -> GameSession:
        normalized = code.upper()
        game_id = self._code_index.get(normalized)
        if game_id:
            return self.get_game(game_id)
        game = self._load_by_code_from_db(normalized)
        if not game:
            raise HTTPException(status_code=404, detail="Partida no encontrada para ese código")
        return game

    def _validate_presenter(self, game: GameSession, presenter_token: str) -> None:
        if game.presenter_token != presenter_token:
            raise HTTPException(status_code=401, detail="Token de presentador inválido")

    def _get_player_by_token(self, game: GameSession, player_token: str) -> Tuple[str, Player]:
        player_id = game.player_tokens.get(player_token)
        if not player_id or player_id not in game.players:
            raise HTTPException(status_code=401, detail="Token de jugador inválido")
        return player_id, game.players[player_id]

    def get_player_for_token(
        self, game_id: str, player_token: str
    ) -> Tuple[GameSession, str, Player]:
        game = self.get_game(game_id)
        player_id, player = self._get_player_by_token(game, player_token)
        return game, player_id, player

    def join_game(
        self,
        code: str,
        player_name: str,
        external_ref: Optional[str] = None,
        client_key: Optional[str] = None,
        auto_rejoin: bool = False,
    ) -> Tuple[GameSession, Player, str]:
        game = self.get_game_by_code(code)
        with self._lock_for(game.id):
            if game.phase == GamePhase.FINISHED:
                raise HTTPException(status_code=400, detail="La partida ha terminado")
            own_keys = [key for key in (external_ref, client_key) if key]
            if any(key in game.kicked_keys for key in own_keys):
                if auto_rejoin:
                    # El móvil intenta reengancharse solo tras perder la
                    # sesión: si lo expulsó el presentador, no se le vuelve
                    # a meter.
                    raise HTTPException(status_code=403, detail="El presentador te ha sacado de la partida")
                # Se vuelve a unir a mano: se le deja (expulsión por error).
                game.kicked_keys = [key for key in game.kicked_keys if key not in own_keys]
            for existing_id, existing in game.players.items():
                if (external_ref and existing.external_ref == external_ref) or (
                    client_key and existing.client_key == client_key
                ):
                    # Ya unido antes (reintento, otra pestaña/dispositivo o
                    # sesión perdida): mismo jugador, token nuevo. Evita
                    # duplicar su puntuación.
                    player_token = _generate_token()
                    game.player_tokens[player_token] = existing_id
                    self._save(game)
                    return game, existing, player_token

            if game.phase not in (GamePhase.LOBBY, GamePhase.QUESTION_WAITING):
                raise HTTPException(status_code=400, detail="La partida ya ha empezado")

            for p in game.players.values():
                if p.name.strip().lower() == player_name.strip().lower():
                    raise HTTPException(status_code=400, detail="Ya hay un jugador con ese nombre")

            player_id = _generate_id("player")
            player_token = _generate_token()
            player = Player(
                id=player_id, name=player_name, external_ref=external_ref, client_key=client_key
            )

            game.players[player_id] = player
            game.player_tokens[player_token] = player_id
            self._save(game)
            return game, player, player_token

    def next_question(self, game_id: str, presenter_token: str, question: Question) -> GameSession:
        with self._lock_for(game_id):
            game = self.get_game(game_id)
            self._validate_presenter(game, presenter_token)
            if game.phase == GamePhase.FINISHED:
                raise HTTPException(status_code=400, detail="La partida está terminada")
            if game.current_question_id == question.id and game.phase in (
                GamePhase.QUESTION_WAITING,
                GamePhase.ANSWERING,
            ):
                # Reintento (o doble clic): ya es la pregunta actual. No se
                # toca nada para no cortar un tiempo de respuesta ya abierto.
                return game

            game.current_question_id = question.id
            game.phase = GamePhase.QUESTION_WAITING
            game.answer_window_started_at = None
            game.answer_duration_seconds = None
            self._save(game)
            return game

    def open_answers(
        self,
        game_id: str,
        presenter_token: str,
        question: Question,
        duration_seconds: int,
    ) -> GameSession:
        with self._lock_for(game_id):
            now = time.time()
            game = self.get_game(game_id)
            self._validate_presenter(game, presenter_token)
            if game.phase == GamePhase.FINISHED:
                raise HTTPException(status_code=400, detail="La partida está terminada")
            if game.current_question_id not in (None, question.id):
                raise HTTPException(
                    status_code=400,
                    detail="La pregunta abierta no coincide con la actual",
                )
            if (
                game.phase == GamePhase.ANSWERING
                and game.current_question_id == question.id
                and self.is_answer_window_open(game)
            ):
                # Reintento (o doble clic) con el tiempo ya corriendo: no se
                # reinicia ni se borran las respuestas que ya han llegado.
                return game

            game.current_question_id = question.id
            game.phase = GamePhase.ANSWERING
            game.answer_window_started_at = now
            game.answer_duration_seconds = duration_seconds
            game.answers[question.id] = {}
            self._save(game)
            return game

    def close_answers(
        self,
        game_id: str,
        presenter_token: str,
        question: Question,
    ) -> QuestionResult:
        with self._lock_for(game_id):
            game = self.get_game(game_id)
            self._validate_presenter(game, presenter_token)
            existing_result = game.question_results.get(question.id)
            if existing_result is not None and game.phase in (
                GamePhase.RESULTS,
                GamePhase.INTERMISSION,
            ):
                # Ya corregida: un reintento no puede volver a sumar ni a
                # dividir puntos.
                return existing_result
            if game.phase not in (GamePhase.ANSWERING, GamePhase.RESULTS, GamePhase.QUESTION_WAITING):
                raise HTTPException(status_code=400, detail="No hay ventana de respuestas abierta")

            answers = game.answers.get(question.id, {})
            correct_option_id = question.get_correct_option_id()
            option_counts: Dict[str, int] = {}
            free_text_samples: list[str] = []
            players_correct: list[str] = []
            players_wrong: list[str] = []
            players_joker: list[str] = []
            players_wrong_names: list[str] = []
            players_joker_names: list[str] = []
            players_correct_names: list[str] = []

            for player_id, player in game.players.items():
                if player.status in (PlayerStatus.ELIMINATED, PlayerStatus.CASHED_OUT) and player_id not in answers:
                    # Ya eliminado o plantado de rondas anteriores: no participa ni cuenta.
                    continue

                record = answers.get(player_id)
                if record and record.used_joker:
                    players_joker.append(player_id)
                    players_joker_names.append(player.name)
                    player.joker_available = False
                    player.joker_used_on_question_id = question.id
                    player.last_answer = None
                    player.last_answer_correct = None
                    continue

                if not record:
                    record = AnswerRecord(
                        player_id=player_id,
                        question_id=question.id,
                        used_joker=False,
                        correct=False,
                        answered_at=None,
                    )
                    answers[player_id] = record

                if question.type == QuestionType.SINGLE_CHOICE:
                    selected = (record.selected_option_id or "").strip().lower()
                    record.correct = (
                        selected == (correct_option_id or "").strip().lower()
                        if correct_option_id
                        else False
                    )
                    key = record.selected_option_id or "none"
                    option_counts[key] = option_counts.get(key, 0) + 1
                else:
                    record.correct = _normalize_free_text(record.text_answer) == _normalize_free_text(
                        question.correct_free_text
                    )
                    if record.text_answer:
                        free_text_samples.append(record.text_answer)

                player.last_answer = record.selected_option_id or record.text_answer
                player.last_answer_correct = record.correct

                if record.correct:
                    players_correct.append(player_id)
                    players_correct_names.append(player.name)
                else:
                    players_wrong.append(player_id)
                    players_wrong_names.append(player.name)
                    if not question.practice:  # en la de prueba nadie queda eliminado
                        player.status = PlayerStatus.ELIMINATED
                player.score = score_after_answer(player.score, question, record.correct)

            answered_records = [
                a for a in answers.values() if not a.used_joker and (a.selected_option_id or a.text_answer)
            ]

            game.answers[question.id] = answers
            game.phase = GamePhase.RESULTS
            game.answer_window_started_at = None
            game.answer_duration_seconds = None
            game.question_results[question.id] = QuestionResult(
                question_id=question.id,
                total_answers=len(answered_records),
                option_counts=option_counts,
                free_text_samples=free_text_samples[:10],
                correct_option_id=correct_option_id,
                correct_free_text=question.correct_free_text,
                players_correct=players_correct,
                players_wrong=players_wrong,
                players_joker=players_joker,
                players_wrong_names=players_wrong_names,
                players_joker_names=players_joker_names,
                players_correct_names=players_correct_names,
            )
            game.score_history = [snap for snap in game.score_history if snap.question_id != question.id]
            if not question.practice:  # la de prueba no es un paso del recuento
                game.score_history.append(
                    ScoreSnapshot(
                        question_id=question.id,
                        scores={pid: p.score for pid, p in game.players.items()},
                    )
                )
            self._save(game)
            return game.question_results[question.id]

    def start_intermission(self, game_id: str, presenter_token: str) -> GameSession:
        with self._lock_for(game_id):
            game = self.get_game(game_id)
            self._validate_presenter(game, presenter_token)
            if game.phase == GamePhase.INTERMISSION:
                return game
            if game.phase not in (GamePhase.RESULTS, GamePhase.QUESTION_WAITING):
                raise HTTPException(status_code=400, detail="Solo puedes mostrar resumen tras corregir")
            game.phase = GamePhase.INTERMISSION
            self._save(game)
            return game

    def record_answer(
        self,
        game_id: str,
        player_token: str,
        question: Question,
        *,
        selected_option_id: Optional[str] = None,
        text_answer: Optional[str] = None,
    ) -> AnswerRecord:
        with self._lock_for(game_id):
            now = time.time()
            game = self.get_game(game_id)
            player_id, player = self._get_player_by_token(game, player_token)

            existing = game.answers.get(question.id, {}).get(player_id)
            if existing is not None and (
                existing.selected_option_id or existing.text_answer or existing.used_joker
            ):
                # Reintento del móvil (la primera sí llegó) o doble toque: se
                # queda la primera respuesta y se confirma sin error.
                return existing

            if game.phase != GamePhase.ANSWERING or game.current_question_id != question.id:
                raise HTTPException(status_code=400, detail="No puedes responder en este momento")

            if not self.is_answer_window_open(game, grace_seconds=ANSWER_GRACE_SECONDS):
                raise HTTPException(status_code=400, detail="El tiempo de respuesta ha terminado")

            if player.status != PlayerStatus.ALIVE:
                raise HTTPException(status_code=400, detail="No puedes responder en tu estado actual")

            answers = game.answers.setdefault(question.id, {})

            record = AnswerRecord(
                player_id=player_id,
                question_id=question.id,
                selected_option_id=selected_option_id,
                text_answer=text_answer,
                used_joker=False,
                answered_at=now,
            )
            answers[player_id] = record
            game.answers[question.id] = answers
            player.last_answer = selected_option_id or text_answer
            player.last_answer_correct = None
            self._save(game)
            return record

    def use_joker(
        self,
        game_id: str,
        player_token: str,
        question: Question,
    ) -> AnswerRecord:
        with self._lock_for(game_id):
            game = self.get_game(game_id)
            player_id, player = self._get_player_by_token(game, player_token)

            existing = game.answers.get(question.id, {}).get(player_id)
            if existing is not None and existing.used_joker:
                return existing  # reintento: el comodín ya quedó registrado

            if question.practice:
                raise HTTPException(status_code=400, detail="En la pregunta de prueba no hay comodín")
            if not player.joker_available:
                raise HTTPException(status_code=400, detail="Ya usaste tu comodín")
            if player.status != PlayerStatus.ALIVE:
                raise HTTPException(status_code=400, detail="No puedes usar el comodín ahora")
            if game.phase != GamePhase.ANSWERING or game.current_question_id != question.id:
                raise HTTPException(status_code=400, detail="No puedes usar el comodín ahora")
            if not self.is_answer_window_open(game, grace_seconds=ANSWER_GRACE_SECONDS):
                raise HTTPException(status_code=400, detail="El tiempo de respuesta ha terminado")

            answers = game.answers.setdefault(question.id, {})
            if existing is not None:
                raise HTTPException(status_code=400, detail="Ya respondiste o usaste el comodín")

            record = AnswerRecord(
                player_id=player_id,
                question_id=question.id,
                used_joker=True,
                answered_at=time.time(),
            )
            answers[player_id] = record
            player.joker_available = False
            player.joker_used_on_question_id = question.id
            self._save(game)
            return record

    def finish_game(self, game_id: str, presenter_token: str) -> GameSession:
        with self._lock_for(game_id):
            game = self.get_game(game_id)
            self._validate_presenter(game, presenter_token)
            if game.phase == GamePhase.FINISHED:
                return game
            game.phase = GamePhase.FINISHED
            game.finished_at = time.time()
            # Se cierran las sesiones de los móviles, pero los jugadores y sus
            # puntos se conservan para el desglose y el recuento final.
            game.player_tokens.clear()
            self._save(game)
            return game

    def start_recount(self, game_id: str, presenter_token: str) -> GameSession:
        with self._lock_for(game_id):
            game = self.get_game(game_id)
            self._validate_presenter(game, presenter_token)
            if game.phase != GamePhase.FINISHED:
                raise HTTPException(status_code=400, detail="Termina la partida antes de hacer el recuento")
            if game.recount_started_at and time.time() - game.recount_started_at < 5:
                return game  # reintento del mismo clic: no reiniciar la animación
            game.recount_started_at = time.time()
            self._save(game)
            return game

    def delete_game(self, game_id: str, presenter_token: str) -> None:
        with self._lock_for(game_id):
            game = self.get_game(game_id)
            self._validate_presenter(game, presenter_token)
            self._deleted.add(game_id)

        # Con el volcado parado, para que no vuelva a escribir la partida
        # justo después de borrarla.
        with self._flush_lock:
            with self._dirty_cond:
                self._dirty.discard(game_id)

            def run():
                with self._pool.connection() as conn:
                    conn.execute("delete from public.elporciento_games where id = %s", (game_id,))

            try:
                with_retry(run, what="borrar partida")
            except Exception as exc:  # noqa: BLE001
                self._deleted.discard(game_id)
                raise HTTPException(
                    status_code=503, detail="Base de datos no disponible, reintenta"
                ) from exc

        self._games.pop(game_id, None)
        self._code_index.pop(game.code.upper(), None)

    def cash_out(
        self,
        game_id: str,
        player_token: str,
        multiplier: float,
        question: Optional[Question] = None,
    ) -> Player:
        with self._lock_for(game_id):
            game = self.get_game(game_id)
            player_id, player = self._get_player_by_token(game, player_token)
            if (
                player.status == PlayerStatus.CASHED_OUT
                and player.cashed_out_multiplier == multiplier
            ):
                return player  # reintento: ya se había plantado
            if player.status != PlayerStatus.ALIVE:
                raise HTTPException(status_code=400, detail="No puedes plantarte en tu estado actual")
            if multiplier <= 0:
                raise HTTPException(status_code=400, detail="Multiplicador inválido")

            if multiplier == 1 and not getattr(game, "cashout_keep_open", False):
                raise HTTPException(status_code=400, detail="Plantarse (mantener) no está abierto")
            if multiplier > 1 and not getattr(game, "cashout_boost_open", False):
                raise HTTPException(status_code=400, detail="Plantarse x1.5 no está abierto")

            player.status = PlayerStatus.CASHED_OUT
            player.cashed_out_multiplier = multiplier
            player.cashed_out_at_question_id = question.id if question else game.current_question_id
            player.score = max(0, player.score * multiplier)
            self._save(game)
            return player

    def set_cashout_window(self, game_id: str, presenter_token: str, *, kind: str, open_state: bool) -> GameSession:
        with self._lock_for(game_id):
            game = self.get_game(game_id)
            self._validate_presenter(game, presenter_token)
            if kind == "keep":
                game.cashout_keep_open = open_state
                if open_state:
                    game.cashout_boost_open = False
            elif kind == "boost":
                game.cashout_boost_open = open_state
                if open_state:
                    game.cashout_keep_open = False
            else:
                raise HTTPException(status_code=400, detail="Tipo de plantarse inválido")
            self._save(game)
            return game

    def leave_game(self, player_token: str) -> Optional[str]:
        for game in list(self._games.values()):
            if player_token in game.player_tokens:
                with self._lock_for(game.id):
                    pid = game.player_tokens.pop(player_token, None)
                    if pid:
                        game.players.pop(pid, None)
                        self._save(game)
                        return game.id
                return None

        row = self._fetch_one(
            "select id from public.elporciento_games where data -> 'player_tokens' ? %s",
            (player_token,),
        )
        if not row:
            return None

        game = self.get_game(row[0])
        with self._lock_for(game.id):
            pid = game.player_tokens.pop(player_token, None)
            if not pid:
                return None
            game.players.pop(pid, None)
            self._save(game)
            return game.id

    def remove_player_by_presenter(self, game_id: str, presenter_token: str, player_id: str) -> None:
        with self._lock_for(game_id):
            game = self.get_game(game_id)
            self._validate_presenter(game, presenter_token)
            # remove token mapping
            tokens_to_delete = [token for token, pid in game.player_tokens.items() if pid == player_id]
            for t in tokens_to_delete:
                game.player_tokens.pop(t, None)
            kicked = game.players.pop(player_id, None)
            if kicked:
                for key in (kicked.external_ref, kicked.client_key):
                    if key and key not in game.kicked_keys:
                        game.kicked_keys.append(key)
            self._save(game)

    def is_answer_window_open(self, game: GameSession, grace_seconds: float = 0.0) -> bool:
        if game.answer_window_started_at is None or game.answer_duration_seconds is None:
            return False
        now = time.time()
        return (now - game.answer_window_started_at) < game.answer_duration_seconds + grace_seconds

    def answer_time_left_ms(self, game: GameSession) -> Optional[int]:
        if game.answer_window_started_at is None or game.answer_duration_seconds is None:
            return None
        now = time.time()
        elapsed = now - game.answer_window_started_at
        remaining = game.answer_duration_seconds - elapsed
        return max(int(remaining * 1000), 0)
