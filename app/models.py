from __future__ import annotations

import time
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class GamePhase(str, Enum):
    LOBBY = "lobby"
    QUESTION_WAITING = "question_waiting"
    ANSWERING = "answering"
    RESULTS = "results"
    INTERMISSION = "intermission"
    FINISHED = "finished"


class QuestionType(str, Enum):
    SINGLE_CHOICE = "single_choice"
    FREE_TEXT = "free_text"


class PlayerStatus(str, Enum):
    ALIVE = "alive"
    ELIMINATED = "eliminated"
    CASHED_OUT = "cashed_out"


class AnswerOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: Optional[str] = None
    image: str
    correct: bool = False


class AnswerOptionPublic(BaseModel):
    id: str
    text: Optional[str] = None
    image: str
    correct: Optional[bool] = None


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    order: int
    type: QuestionType
    text: str
    image: str
    time_limit_seconds: int = Field(..., gt=0)
    points: int = Field(0, ge=0)
    options: Optional[List[AnswerOption]] = None
    correct_free_text: Optional[str] = None

    def get_correct_option_id(self) -> Optional[str]:
        if not self.options:
            return None
        for opt in self.options:
            if opt.correct:
                return opt.id
        return None

    def to_public(self, include_correct: bool = False) -> "QuestionPublic":
        options_public: Optional[List[AnswerOptionPublic]] = None
        if self.options is not None:
            options_public = [
                AnswerOptionPublic(
                    id=opt.id,
                    text=opt.text,
                    image=opt.image,
                    correct=opt.correct if include_correct else None,
                )
                for opt in self.options
            ]

        return QuestionPublic(
            id=self.id,
            order=self.order,
            type=self.type,
            text=self.text,
            image=self.image,
            time_limit_seconds=self.time_limit_seconds,
            points=self.points,
            options=options_public,
            correct_option_id=self.get_correct_option_id() if include_correct else None,
            correct_free_text=self.correct_free_text if include_correct else None,
        )


class QuestionPublic(BaseModel):
    id: str
    order: int
    type: QuestionType
    text: str
    image: str
    time_limit_seconds: int
    points: int
    options: Optional[List[AnswerOptionPublic]] = None
    correct_option_id: Optional[str] = None
    correct_free_text: Optional[str] = None


class Player(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    status: PlayerStatus = PlayerStatus.ALIVE
    joker_available: bool = True
    joker_used_on_question_id: Optional[str] = None
    score: float = 0
    cashed_out_at_question_id: Optional[str] = None
    cashed_out_multiplier: Optional[float] = None
    last_answer: Optional[str] = None
    last_answer_correct: Optional[bool] = None


class PlayerForPresenter(BaseModel):
    id: str
    name: str
    status: PlayerStatus
    joker_available: bool
    joker_used_on_question_id: Optional[str]
    score: float
    last_answer: Optional[str]
    last_answer_correct: Optional[bool]


class PlayerForScreen(BaseModel):
    id: str
    name: str
    status: PlayerStatus


class PlayerState(BaseModel):
    game_id: str
    phase: GamePhase
    status: PlayerStatus
    current_question: Optional[QuestionPublic] = None
    can_answer: bool
    has_answered: bool
    joker_available: bool
    alive: bool
    can_cashout_keep: bool
    can_cashout_boost: bool
    score: float
    cashed_out_multiplier: Optional[float] = None
    answer_time_left_ms: Optional[int] = None
    last_answer_correct: Optional[bool] = None
    last_answer: Optional[str] = None


class AnswerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player_id: str
    question_id: str
    selected_option_id: Optional[str] = None
    text_answer: Optional[str] = None
    used_joker: bool = False
    correct: Optional[bool] = None
    answered_at: Optional[float] = None


class QuestionResult(BaseModel):
    question_id: str
    total_answers: int
    option_counts: Dict[str, int]
    free_text_samples: List[str] = Field(default_factory=list)
    correct_option_id: Optional[str] = None
    correct_free_text: Optional[str] = None
    players_correct: List[str] = Field(default_factory=list)
    players_wrong: List[str] = Field(default_factory=list)
    players_joker: List[str] = Field(default_factory=list)
    players_wrong_names: List[str] = Field(default_factory=list)
    players_joker_names: List[str] = Field(default_factory=list)
    players_correct_names: List[str] = Field(default_factory=list)


class GameSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    code: str
    presenter_id: str
    presenter_name: str
    presenter_token: str
    player_tokens: Dict[str, str] = Field(default_factory=dict)
    phase: GamePhase = GamePhase.LOBBY
    current_question_id: Optional[str] = None
    answer_window_started_at: Optional[float] = None
    answer_duration_seconds: Optional[int] = None
    cashout_keep_open: bool = False
    cashout_boost_open: bool = False
    # Compatibilidad con partidas guardadas antiguas
    cashout_open: Optional[bool] = None
    created_at: float = Field(default_factory=lambda: time.time())
    finished_at: Optional[float] = None
    players: Dict[str, Player] = Field(default_factory=dict)
    answers: Dict[str, Dict[str, AnswerRecord]] = Field(default_factory=dict)
    question_results: Dict[str, QuestionResult] = Field(default_factory=dict)

    def alive_players(self) -> List[Player]:
        return [p for p in self.players.values() if p.status == PlayerStatus.ALIVE]

    def total_players(self) -> int:
        return len(self.players)

    def get_answers_for_question(self, question_id: str) -> Dict[str, AnswerRecord]:
        return self.answers.get(question_id, {})
