from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import yaml
from fastapi import HTTPException

from .models import Question, QuestionType


class QuestionStore:
    """Carga y valida preguntas desde YAML."""

    def __init__(self, yaml_path: Path):
        self.yaml_path = yaml_path
        self._questions: List[Question] = []
        self.reload()

    def reload(self) -> None:
        if not self.yaml_path.exists():
            raise FileNotFoundError(f"No se encontró el archivo de preguntas: {self.yaml_path}")

        raw = yaml.safe_load(self.yaml_path.read_text(encoding="utf-8")) or {}
        items = raw.get("questions") if isinstance(raw, dict) else None
        if not items:
            raise ValueError("questions.yaml debe contener una lista en la clave 'questions'")

        parsed: List[Question] = []
        seen_ids = set()
        seen_orders = set()
        for item in items:
            question = Question(**item)
            if question.id in seen_ids:
                raise ValueError(f"ID duplicado de pregunta: {question.id}")
            if question.order in seen_orders:
                raise ValueError(f"Orden duplicado de pregunta: {question.order}")

            if question.type == QuestionType.SINGLE_CHOICE:
                if not question.options:
                    raise ValueError(f"La pregunta {question.id} debe tener opciones")
                correct = [opt for opt in question.options if opt.correct]
                if len(correct) != 1:
                    raise ValueError(
                        f"La pregunta {question.id} debe tener exactamente 1 opción correcta"
                    )
            elif question.type == QuestionType.FREE_TEXT:
                if not question.correct_free_text:
                    raise ValueError(
                        f"La pregunta {question.id} necesita 'correct_free_text' para respuestas abiertas"
                    )

            seen_ids.add(question.id)
            seen_orders.add(question.order)
            parsed.append(question)

        parsed.sort(key=lambda q: q.order)
        self._questions = parsed

    def all_questions(self) -> List[Question]:
        return list(self._questions)

    def get_by_id(self, question_id: str) -> Question:
        for q in self._questions:
            if q.id == question_id:
                return q
        raise HTTPException(status_code=404, detail="Pregunta no encontrada")

    def get_first(self) -> Question:
        if not self._questions:
            raise HTTPException(status_code=404, detail="No hay preguntas cargadas")
        return self._questions[0]

    def get_next_after(self, question_id: str) -> Question:
        for idx, q in enumerate(self._questions):
            if q.id == question_id and idx + 1 < len(self._questions):
                return self._questions[idx + 1]
        raise HTTPException(status_code=404, detail="No hay más preguntas después de esa")

    def resolve_question(self, question_id: Optional[str]) -> Question:
        if question_id:
            return self.get_by_id(question_id)
        return self.get_first()
