from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .game_store import GameStore
from .models import (
    AnswerRecord,
    GamePhase,
    GameSession,
    PlayerForPresenter,
    PlayerState,
    PlayerStatus,
    QuestionPublic,
    QuestionResult,
    QuestionType,
)
from .question_store import QuestionStore

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
QUESTIONS_PATH = DATA_DIR / "questions.yaml"
GAMES_DIR = DATA_DIR / "games"
STATIC_DIR = BASE_DIR.parent / "static"

question_store = QuestionStore(QUESTIONS_PATH)
game_store = GameStore(GAMES_DIR)

app = FastAPI(
    title="El 1% - Backend",
    description="API para partidas tipo \"El 1%\" con preguntas en YAML y partidas en JSON.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class CreateGameRequest(BaseModel):
    presenter_name: str = Field(..., min_length=1)


class NextQuestionRequest(BaseModel):
    presenter_token: str
    question_id: Optional[str] = None


class OpenAnswersRequest(BaseModel):
    presenter_token: str
    question_id: Optional[str] = None
    duration_seconds: Optional[int] = Field(None, gt=0)


class CloseAnswersRequest(BaseModel):
    presenter_token: str
    question_id: Optional[str] = None


class FinishGameRequest(BaseModel):
    presenter_token: str


class JoinGameRequest(BaseModel):
    code: str
    player_name: str


class SubmitAnswerRequest(BaseModel):
    player_token: str
    question_id: str
    selected_option_id: Optional[str] = None
    text_answer: Optional[str] = None


class UseJokerRequest(BaseModel):
    player_token: str
    question_id: str


class CashOutRequest(BaseModel):
    player_token: str
    multiplier: float = Field(..., gt=0)


class CashOutToggleRequest(BaseModel):
    presenter_token: str
    open: bool
    kind: str = Field(..., pattern="^(keep|boost)$")


class KickPlayerRequest(BaseModel):
    presenter_token: str
    player_id: str


class IntermissionRequest(BaseModel):
    presenter_token: str


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "questions": len(question_store.all_questions()),
    }


@app.get("/api/questions")
def list_questions(include_correct: bool = Query(False)) -> list[QuestionPublic]:
    return [q.to_public(include_correct=include_correct) for q in question_store.all_questions()]


@app.get("/api/questions/{question_id}")
def get_question(question_id: str, include_correct: bool = Query(False)) -> QuestionPublic:
    q = question_store.get_by_id(question_id)
    return q.to_public(include_correct=include_correct)


@app.get("/api/questions/first")
def get_first_question(include_correct: bool = Query(False)) -> QuestionPublic:
    q = question_store.get_first()
    return q.to_public(include_correct=include_correct)


@app.get("/api/questions/{question_id}/next")
def get_next_question(question_id: str, include_correct: bool = Query(False)) -> QuestionPublic:
    q = question_store.get_next_after(question_id)
    return q.to_public(include_correct=include_correct)


@app.post("/api/games")
def create_game(payload: CreateGameRequest) -> dict:
    game = game_store.create_game(payload.presenter_name)
    return {
        "game_id": game.id,
        "code": game.code,
        "presenter_id": game.presenter_id,
        "presenter_token": game.presenter_token,
        "phase": game.phase,
        "question_count": len(question_store.all_questions()),
    }


def _require_presenter_token(game: GameSession, presenter_token: str) -> None:
    if game.presenter_token != presenter_token:
        raise HTTPException(status_code=401, detail="Token de presentador inválido")


@app.get("/api/games/{game_id}/presenter/state")
def presenter_state(game_id: str, presenter_token: str = Query(...)) -> dict:
    game = game_store.get_game(game_id)
    _require_presenter_token(game, presenter_token)

    question_public = None
    if game.current_question_id:
        question_public = question_store.get_by_id(game.current_question_id).to_public()

    answer_time_left = game_store.answer_time_left_ms(game)
    players_view = [
        PlayerForPresenter(
            id=p.id,
            name=p.name,
            status=p.status,
            joker_available=p.joker_available,
            joker_used_on_question_id=p.joker_used_on_question_id,
            score=p.score,
            last_answer=p.last_answer,
            last_answer_correct=p.last_answer_correct,
        )
        for p in game.players.values()
    ]

    return {
        "game_id": game.id,
        "code": game.code,
        "phase": game.phase,
        "current_question": question_public,
        "answer_time_left_ms": answer_time_left,
        "players": players_view,
        "cashout_keep_open": game.cashout_keep_open,
        "cashout_boost_open": game.cashout_boost_open,
    }


@app.get("/api/games/{game_id}/screen/state")
def screen_state(game_id: str) -> dict:
    game = game_store.get_game(game_id)
    question_public = None
    if game.current_question_id:
        question_public = question_store.get_by_id(game.current_question_id).to_public()
    last_result = None
    if game.phase in (GamePhase.RESULTS, GamePhase.INTERMISSION) and game.current_question_id:
        result = game.question_results.get(game.current_question_id)
        if result:
            last_result = result if isinstance(result, dict) else result.model_dump()

    intermission_alive = []
    intermission_eliminated = []
    if game.phase == GamePhase.INTERMISSION:
        last_wrong = set((last_result or {}).get("players_wrong") or [])
        for pid, player in game.players.items():
            entry = {
                "name": player.name,
                "score": player.score,
                "joker_available": player.joker_available,
                "status": player.status,
                "last_eliminated": pid in last_wrong,
                "cashed_out": player.status == PlayerStatus.CASHED_OUT,
            }
            if player.status == PlayerStatus.ALIVE:
                intermission_alive.append(entry)
            else:
                intermission_eliminated.append(entry)

    return {
        "game_id": game.id,
        "code": game.code,
        "phase": game.phase,
        "current_question": question_public,
        "alive_count": len(game.alive_players()),
        "total_players": game.total_players(),
        "answer_time_left_ms": game_store.answer_time_left_ms(game),
        "cashout_keep_open": game.cashout_keep_open,
        "cashout_boost_open": game.cashout_boost_open,
        "last_result": last_result,
        "intermission_alive": intermission_alive,
        "intermission_eliminated": intermission_eliminated,
    }


@app.post("/api/games/{game_id}/next-question")
def next_question(game_id: str, payload: NextQuestionRequest) -> dict:
    game = game_store.get_game(game_id)
    _require_presenter_token(game, payload.presenter_token)

    if payload.question_id:
        question = question_store.get_by_id(payload.question_id)
    elif game.current_question_id:
        question = question_store.get_next_after(game.current_question_id)
    else:
        question = question_store.get_first()

    updated_game = game_store.next_question(game_id, payload.presenter_token, question)
    return {
        "game_id": updated_game.id,
        "phase": updated_game.phase,
        "current_question": question.to_public(include_correct=True),
    }


@app.post("/api/games/{game_id}/open-answers")
def open_answers(game_id: str, payload: OpenAnswersRequest) -> dict:
    game = game_store.get_game(game_id)
    _require_presenter_token(game, payload.presenter_token)
    # salir de intermission cuando se abre nueva ventana
    if game.phase == GamePhase.INTERMISSION:
        game.phase = GamePhase.ANSWERING

    if payload.question_id:
        question = question_store.get_by_id(payload.question_id)
    elif game.current_question_id:
        question = question_store.get_by_id(game.current_question_id)
    else:
        question = question_store.get_first()

    duration = payload.duration_seconds or question.time_limit_seconds
    updated_game = game_store.open_answers(
        game_id, payload.presenter_token, question, duration_seconds=duration
    )
    return {
        "game_id": updated_game.id,
        "phase": updated_game.phase,
        "current_question": question.to_public(),
        "duration_seconds": duration,
    }


@app.post("/api/games/{game_id}/close-answers", response_model=QuestionResult)
def close_answers(game_id: str, payload: CloseAnswersRequest) -> QuestionResult:
    game = game_store.get_game(game_id)
    _require_presenter_token(game, payload.presenter_token)

    if payload.question_id:
        question = question_store.get_by_id(payload.question_id)
    elif game.current_question_id:
        question = question_store.get_by_id(game.current_question_id)
    else:
        raise HTTPException(status_code=400, detail="No hay pregunta seleccionada")

    return game_store.close_answers(game_id, payload.presenter_token, question)


@app.get("/api/games/{game_id}/questions/{question_id}/results", response_model=QuestionResult)
def question_results(
    game_id: str,
    question_id: str,
    presenter_token: str = Query(...),
) -> QuestionResult:
    game = game_store.get_game(game_id)
    _require_presenter_token(game, presenter_token)
    question = question_store.get_by_id(question_id)
    if question_id in game.question_results:
        result = game.question_results[question_id]
        return result if isinstance(result, QuestionResult) else QuestionResult(**result)
    return game_store.close_answers(game_id, presenter_token, question)


@app.post("/api/games/{game_id}/finish")
def finish_game(game_id: str, payload: FinishGameRequest) -> dict:
    updated_game = game_store.finish_game(game_id, payload.presenter_token)
    return {"game_id": updated_game.id, "phase": updated_game.phase}


@app.post("/api/games/join")
def join_game(payload: JoinGameRequest) -> dict:
    game, player, player_token = game_store.join_game(payload.code.upper(), payload.player_name)
    return {
        "game_id": game.id,
        "player_id": player.id,
        "player_token": player_token,
        "code": game.code,
    }


@app.get("/api/games/{game_id}/player/state")
def player_state(game_id: str, player_token: str = Query(...)) -> PlayerState:
    game, player_id, player = game_store.get_player_for_token(game_id, player_token)

    question_public = None
    has_answered = False
    if game.current_question_id:
        question_public = question_store.get_by_id(game.current_question_id).to_public()
        answers = game.answers.get(game.current_question_id, {})
        has_answered = player_id in answers

    return PlayerState(
        game_id=game.id,
        phase=game.phase,
        status=player.status,
        current_question=question_public,
        can_answer=game.phase == GamePhase.ANSWERING
        and game_store.is_answer_window_open(game)
        and player.status == PlayerStatus.ALIVE,
        has_answered=has_answered,
        joker_available=player.joker_available,
        alive=player.status == PlayerStatus.ALIVE,
        can_cashout_keep=game.cashout_keep_open and player.status == PlayerStatus.ALIVE,
        can_cashout_boost=game.cashout_boost_open and player.status == PlayerStatus.ALIVE,
        score=player.score,
        cashed_out_multiplier=player.cashed_out_multiplier,
        answer_time_left_ms=game_store.answer_time_left_ms(game),
        last_answer_correct=player.last_answer_correct,
        last_answer=player.last_answer,
    )


@app.post("/api/games/{game_id}/answer")
def submit_answer(game_id: str, payload: SubmitAnswerRequest) -> AnswerRecord:
    question = question_store.get_by_id(payload.question_id)
    if question.type == QuestionType.SINGLE_CHOICE and not payload.selected_option_id:
        raise HTTPException(status_code=400, detail="Debes enviar selected_option_id")
    if question.type == QuestionType.FREE_TEXT and payload.text_answer is None:
        raise HTTPException(status_code=400, detail="Debes enviar text_answer")

    return game_store.record_answer(
        game_id,
        payload.player_token,
        question,
        selected_option_id=payload.selected_option_id,
        text_answer=payload.text_answer,
    )


@app.post("/api/games/{game_id}/joker")
def use_joker(game_id: str, payload: UseJokerRequest) -> AnswerRecord:
    question = question_store.get_by_id(payload.question_id)
    return game_store.use_joker(game_id, payload.player_token, question)


@app.post("/api/games/{game_id}/cashout")
def cash_out(game_id: str, payload: CashOutRequest) -> dict:
    game = game_store.get_game(game_id)
    question = question_store.get_by_id(game.current_question_id) if game.current_question_id else None
    player = game_store.cash_out(game_id, payload.player_token, payload.multiplier, question)
    return {"player_id": player.id, "score": player.score, "status": player.status}


@app.post("/api/games/{game_id}/cashout/toggle")
def toggle_cashout(game_id: str, payload: CashOutToggleRequest) -> dict:
    game = game_store.set_cashout_window(game_id, payload.presenter_token, kind=payload.kind, open_state=payload.open)
    return {
        "game_id": game.id,
        "cashout_keep_open": game.cashout_keep_open,
        "cashout_boost_open": game.cashout_boost_open,
    }


@app.post("/api/games/{game_id}/players/kick")
def kick_player(game_id: str, payload: KickPlayerRequest) -> dict:
    game_store.remove_player_by_presenter(game_id, payload.presenter_token, payload.player_id)
    return {"status": "ok", "player_id": payload.player_id}


@app.post("/api/games/{game_id}/leave")
def leave_game(game_id: str, player_token: str = Query(...)) -> dict:
    removed_game_id = game_store.leave_game(player_token)
    if removed_game_id is None:
        raise HTTPException(status_code=404, detail="Jugador no encontrado")
    return {"status": "ok", "game_id": removed_game_id}


@app.post("/api/games/{game_id}/intermission")
def start_intermission(game_id: str, payload: IntermissionRequest) -> dict:
    game = game_store.start_intermission(game_id, payload.presenter_token)
    return {"game_id": game.id, "phase": game.phase}


# Frontend entrypoints

def _serve_static_file(filename: str) -> FileResponse:
    path = STATIC_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(path)


@app.get("/", include_in_schema=False)
def serve_player() -> FileResponse:
    return _serve_static_file("index.html")


@app.get("/presenter", include_in_schema=False)
def serve_presenter() -> FileResponse:
    return _serve_static_file("presenter.html")


@app.get("/screen", include_in_schema=False)
def serve_screen() -> FileResponse:
    return _serve_static_file("screen.html")
