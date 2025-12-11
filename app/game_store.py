from __future__ import annotations

import json
import secrets
import string
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from .models import (
    AnswerRecord,
    GamePhase,
    GameSession,
    Player,
    PlayerStatus,
    Question,
    QuestionResult,
    QuestionType,
)


def _generate_id(prefix: str) -> str:
    suffix = secrets.token_hex(4)
    return f"{prefix}_{suffix}"


def _generate_code(length: int = 4) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _generate_token() -> str:
    return secrets.token_urlsafe(24)


def _normalize_free_text(value: Optional[str]) -> str:
    return (value or "").strip().lower()


class GameStore:
    """Gestiona partidas guardadas en JSON dentro de data/games."""

    def __init__(self, games_dir: Path):
        self.games_dir = games_dir
        self.games_dir.mkdir(parents=True, exist_ok=True)
        self._games: Dict[str, GameSession] = {}
        self._code_index: Dict[str, str] = {}

    def _game_file(self, game_id: str) -> Path:
        return self.games_dir / f"game_{game_id}.json"

    def _save(self, game: GameSession) -> None:
        data = jsonable_encoder(game)
        self._game_file(game.id).write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def _load_from_disk(self, game_id: str) -> Optional[GameSession]:
        path = self._game_file(game_id)
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        # Compat: migrar cashout_open legacy a keep/boost si no existen
        if "cashout_open" in raw:
            raw.setdefault("cashout_keep_open", raw.get("cashout_open", False))
            raw.setdefault("cashout_boost_open", raw.get("cashout_open", False))
        game = GameSession(**raw)
        self._games[game.id] = game
        self._code_index[game.code.upper()] = game.id
        return game

    def _load_by_code(self, code: str) -> Optional[GameSession]:
        normalized = code.upper()
        for path in self.games_dir.glob("game_*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if raw.get("code", "").upper() == normalized:
                if "cashout_open" in raw:
                    raw.setdefault("cashout_keep_open", raw.get("cashout_open", False))
                    raw.setdefault("cashout_boost_open", raw.get("cashout_open", False))
                game = GameSession(**raw)
                self._games[game.id] = game
                self._code_index[game.code.upper()] = game.id
                return game
        return None

    def create_game(self, presenter_name: str) -> GameSession:
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
        )
        self._games[game.id] = game
        self._code_index[game.code.upper()] = game.id
        self._save(game)
        return game

    def get_game(self, game_id: str) -> GameSession:
        game = self._games.get(game_id) or self._load_from_disk(game_id)
        if not game:
            raise HTTPException(status_code=404, detail="Partida no encontrada")
        return game

    def get_game_by_code(self, code: str) -> GameSession:
        normalized = code.upper()
        game_id = self._code_index.get(normalized)
        if game_id:
            return self.get_game(game_id)
        game = self._load_by_code(normalized)
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

    def join_game(self, code: str, player_name: str) -> Tuple[GameSession, Player, str]:
        game = self.get_game_by_code(code)
        if game.phase not in (GamePhase.LOBBY, GamePhase.QUESTION_WAITING):
            raise HTTPException(status_code=400, detail="La partida ya ha empezado")

        for p in game.players.values():
            if p.name.strip().lower() == player_name.strip().lower():
                raise HTTPException(status_code=400, detail="Ya hay un jugador con ese nombre")

        player_id = _generate_id("player")
        player_token = _generate_token()
        player = Player(id=player_id, name=player_name)

        game.players[player_id] = player
        game.player_tokens[player_token] = player_id
        self._save(game)
        return game, player, player_token

    def next_question(self, game_id: str, presenter_token: str, question: Question) -> GameSession:
        game = self.get_game(game_id)
        self._validate_presenter(game, presenter_token)
        if game.phase == GamePhase.FINISHED:
            raise HTTPException(status_code=400, detail="La partida está terminada")

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
        game = self.get_game(game_id)
        self._validate_presenter(game, presenter_token)
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
                player.score += question.points
            else:
                players_wrong.append(player_id)
                players_wrong_names.append(player.name)
                player.status = PlayerStatus.ELIMINATED
                player.score = max(0, player.score * 0.5)

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
        self._save(game)
        return game.question_results[question.id]

    def start_intermission(self, game_id: str, presenter_token: str) -> GameSession:
        game = self.get_game(game_id)
        self._validate_presenter(game, presenter_token)
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
        now = time.time()
        game = self.get_game(game_id)
        player_id, player = self._get_player_by_token(game, player_token)

        if game.phase != GamePhase.ANSWERING or game.current_question_id != question.id:
            raise HTTPException(status_code=400, detail="No puedes responder en este momento")

        if not self.is_answer_window_open(game):
            raise HTTPException(status_code=400, detail="El tiempo de respuesta ha terminado")

        if player.status != PlayerStatus.ALIVE:
            raise HTTPException(status_code=400, detail="No puedes responder en tu estado actual")

        answers = game.answers.setdefault(question.id, {})
        if player_id in answers:
            raise HTTPException(status_code=400, detail="Ya respondiste o usaste comodín")

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
        game = self.get_game(game_id)
        player_id, player = self._get_player_by_token(game, player_token)

        if not player.joker_available:
            raise HTTPException(status_code=400, detail="Ya usaste tu comodín")
        if player.status != PlayerStatus.ALIVE:
            raise HTTPException(status_code=400, detail="No puedes usar el comodín ahora")
        if game.phase != GamePhase.ANSWERING or game.current_question_id != question.id:
            raise HTTPException(status_code=400, detail="No puedes usar el comodín ahora")
        if not self.is_answer_window_open(game):
            raise HTTPException(status_code=400, detail="El tiempo de respuesta ha terminado")

        answers = game.answers.setdefault(question.id, {})
        if player_id in answers:
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
        game = self.get_game(game_id)
        self._validate_presenter(game, presenter_token)
        game.phase = GamePhase.FINISHED
        game.finished_at = time.time()
        # Expulsar a todos los jugadores (limpiar tokens)
        game.player_tokens.clear()
        game.players.clear()
        self._save(game)
        return game

    def cash_out(
        self,
        game_id: str,
        player_token: str,
        multiplier: float,
        question: Optional[Question] = None,
    ) -> Player:
        game = self.get_game(game_id)
        player_id, player = self._get_player_by_token(game, player_token)
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
        # Returns game_id if removed
        for game in self._games.values():
            pid = game.player_tokens.get(player_token)
            if pid:
                game.players.pop(pid, None)
                game.player_tokens.pop(player_token, None)
                self._save(game)
                return game.id
        # Try disk
        for path in self.games_dir.glob("game_*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            token_map = raw.get("player_tokens", {})
            pid = token_map.get(player_token)
            if pid:
                token_map.pop(player_token, None)
                players = raw.get("players", {})
                players.pop(pid, None)
                raw["player_tokens"] = token_map
                raw["players"] = players
                path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))
                self._games.pop(raw.get("id"), None)
                self._code_index.pop(raw.get("code", "").upper(), None)
                return raw.get("id")
        return None

    def remove_player_by_presenter(self, game_id: str, presenter_token: str, player_id: str) -> None:
        game = self.get_game(game_id)
        self._validate_presenter(game, presenter_token)
        # remove token mapping
        tokens_to_delete = [token for token, pid in game.player_tokens.items() if pid == player_id]
        for t in tokens_to_delete:
            game.player_tokens.pop(t, None)
        game.players.pop(player_id, None)
        self._save(game)

    def is_answer_window_open(self, game: GameSession) -> bool:
        if game.answer_window_started_at is None or game.answer_duration_seconds is None:
            return False
        now = time.time()
        return (now - game.answer_window_started_at) < game.answer_duration_seconds

    def answer_time_left_ms(self, game: GameSession) -> Optional[int]:
        if game.answer_window_started_at is None or game.answer_duration_seconds is None:
            return None
        now = time.time()
        elapsed = now - game.answer_window_started_at
        remaining = game.answer_duration_seconds - elapsed
        return max(int(remaining * 1000), 0)
