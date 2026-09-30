from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from .db import get_pool, with_retry
from .models import AnswerOption, Question, QuestionPayload, QuestionType

log = logging.getLogger("el1porciento.questions")


def _validate_batch(questions: List[Question]) -> None:
    seen_ids = set()
    seen_orders = set()
    for question in questions:
        if question.id in seen_ids:
            raise ValueError(f"ID duplicado de pregunta: {question.id}")
        if question.order in seen_orders:
            raise ValueError(f"Orden duplicado de pregunta: {question.order}")

        if question.type == QuestionType.SINGLE_CHOICE:
            if not question.options or len(question.options) < 2:
                raise ValueError(f"La pregunta {question.id} debe tener al menos 2 opciones")
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


def _row_to_question(row: dict) -> Question:
    options = None
    if row["options"]:
        options = [
            AnswerOption(
                id=opt["id"],
                text=opt.get("text"),
                image=opt["image_url"],
                correct=opt.get("correct", False),
            )
            for opt in row["options"]
        ]

    return Question(
        id=row["id"],
        order=row["sort_order"],
        type=row["type"],
        text=row["text"],
        image=row["image_url"],
        time_limit_seconds=row["time_limit_seconds"],
        points=row["points"],
        options=options,
        correct_free_text=row["correct_free_text"],
        numeric_answer=row["numeric_answer"],
        usual_correct_percentage=row["usual_correct_percentage"],
        practice=bool(row.get("practice")),
    )


def _payload_to_row_values(payload: QuestionPayload) -> tuple:
    options_json = (
        Jsonb(
            [
                {
                    "id": opt.id,
                    "text": opt.text,
                    "image_url": opt.image_url,
                    "correct": opt.correct,
                }
                for opt in payload.options
            ]
        )
        if payload.options
        else None
    )

    return (
        payload.id,
        payload.sort_order,
        payload.type.value,
        payload.text,
        payload.image_url,
        payload.points,
        payload.time_limit_seconds,
        payload.correct_free_text,
        payload.numeric_answer,
        payload.usual_correct_percentage,
        options_json,
    )


# Pregunta de prueba de ejemplo, creada una sola vez junto con la columna
# practice (se edita o se borra luego desde el admin).
_SEED_PRACTICE_SQL = """
insert into public.elporciento_questions
  (id, sort_order, type, text, image_url, points, time_limit_seconds, options, practice)
select 'prueba',
       coalesce((select min(sort_order) from public.elporciento_questions), 1) - 1,
       'single_choice',
       '¿Quiénes se casan?',
       null, 0, 20,
       '[
         {"id":"a","text":"Iñaki y Marta","image_url":null,"correct":true},
         {"id":"b","text":"Pepe y Lola","image_url":null,"correct":false},
         {"id":"c","text":"Nadie, es una trampa","image_url":null,"correct":false},
         {"id":"d","text":"Todos los invitados","image_url":null,"correct":false}
       ]'::jsonb,
       true
on conflict (id) do nothing
"""


class QuestionStore:
    """Banco de preguntas persistido en Postgres (tabla
    public.elporciento_questions), con caché en memoria igual que antes
    tenía el YAML -- se refresca con `reload()` tras cualquier escritura
    desde el admin.
    """

    def __init__(self) -> None:
        self._pool = get_pool()
        self._questions: List[Question] = []
        self._has_practice_column = False
        with_retry(self._ensure_practice_column, attempts=8, what="preparar columna de prueba")
        # Al arrancar (p. ej. Render reiniciando el proceso) la base puede
        # tardar en responder: se reintenta en vez de caerse a la primera.
        with_retry(self.reload, attempts=8, what="cargar preguntas")

    def _ensure_practice_column(self) -> None:
        """La marca de "pregunta de prueba" es una columna nueva. La app la
        crea sola si falta (no toca nada existente), para no depender de
        aplicar la migración a mano en Supabase antes de desplegar. Si el
        usuario de la base no tuviera permiso, todo sigue funcionando igual
        que antes, solo que sin preguntas de prueba.

        La primera vez (cuando la columna se acaba de crear) añade también una
        pregunta de prueba de ejemplo, la primera del orden, para editarla
        desde el admin."""
        exists_sql = """
            select 1 from information_schema.columns
            where table_schema = 'public' and table_name = 'elporciento_questions'
              and column_name = 'practice'
        """
        with self._pool.connection() as conn:
            existed = bool(conn.execute(exists_sql).fetchone())
            if not existed:
                try:
                    conn.execute(
                        "alter table public.elporciento_questions "
                        "add column if not exists practice boolean not null default false"
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("No se pudo crear la columna practice: %s", exc)
            self._has_practice_column = bool(conn.execute(exists_sql).fetchone())
            if self._has_practice_column and not existed:
                conn.execute(_SEED_PRACTICE_SQL)

    def _set_practice(self, question_id: str, practice: bool) -> None:
        if not self._has_practice_column:
            if practice:
                raise HTTPException(
                    status_code=400,
                    detail="La base de datos aún no admite preguntas de prueba (falta la columna practice)",
                )
            return
        with self._pool.connection() as conn:
            conn.execute(
                "update public.elporciento_questions set practice = %s where id = %s",
                (practice, question_id),
            )

    def reload(self) -> None:
        practice_expr = "practice" if self._has_practice_column else "false"
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""
                select id, sort_order, type, text, image_url, points,
                       time_limit_seconds, correct_free_text, numeric_answer,
                       usual_correct_percentage, options, {practice_expr}
                from public.elporciento_questions
                order by sort_order asc
                """
            ).fetchall()

        columns = [
            "id",
            "sort_order",
            "type",
            "text",
            "image_url",
            "points",
            "time_limit_seconds",
            "correct_free_text",
            "numeric_answer",
            "usual_correct_percentage",
            "options",
            "practice",
        ]
        parsed = [_row_to_question(dict(zip(columns, row))) for row in rows]
        _validate_batch(parsed)
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

    def create(self, payload: QuestionPayload) -> Question:
        with self._pool.connection() as conn:
            exists = conn.execute(
                "select 1 from public.elporciento_questions where id = %s", (payload.id,)
            ).fetchone()
            if exists:
                raise HTTPException(status_code=400, detail="Ya existe una pregunta con ese ID")

            conn.execute(
                """
                insert into public.elporciento_questions
                  (id, sort_order, type, text, image_url, points, time_limit_seconds, correct_free_text, numeric_answer, usual_correct_percentage, options)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                _payload_to_row_values(payload),
            )

        try:
            self._set_practice(payload.id, payload.practice)
        except HTTPException:
            self.delete(payload.id, _skip_reload=True)
            raise

        try:
            self.reload()
        except ValueError as exc:
            # Deshacer si el lote completo queda inconsistente (ids/orden
            # duplicados) -- mejor fallar la creación que dejar el banco de
            # preguntas roto para las partidas en curso.
            self.delete(payload.id, _skip_reload=True)
            self.reload()
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return self.get_by_id(payload.id)

    def update(self, question_id: str, payload: QuestionPayload) -> Question:
        if payload.id != question_id:
            raise HTTPException(status_code=400, detail="No se puede cambiar el ID de una pregunta")

        previous = self.get_by_id(question_id)  # 404 si no existe, y sirve para poder revertir
        options_json = _payload_to_row_values(payload)[-1]

        def _apply(values: tuple) -> None:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    update public.elporciento_questions
                    set sort_order = %s, type = %s, text = %s, image_url = %s, points = %s,
                        time_limit_seconds = %s, correct_free_text = %s, numeric_answer = %s,
                        usual_correct_percentage = %s, options = %s,
                        updated_at = now()
                    where id = %s
                    """,
                    values,
                )

        _apply(
            (
                payload.sort_order,
                payload.type.value,
                payload.text,
                payload.image_url,
                payload.points,
                payload.time_limit_seconds,
                payload.correct_free_text,
                payload.numeric_answer,
                payload.usual_correct_percentage,
                options_json,
                question_id,
            )
        )
        self._set_practice(question_id, payload.practice)

        try:
            self.reload()
        except ValueError as exc:
            # El lote entero queda inconsistente (p. ej. orden duplicado con
            # otra pregunta) -- revertir a los valores anteriores en vez de
            # dejar el banco de preguntas roto para las partidas en curso.
            previous_options_json = (
                Jsonb(
                    [
                        {"id": o.id, "text": o.text, "image_url": o.image, "correct": o.correct}
                        for o in previous.options
                    ]
                )
                if previous.options
                else None
            )
            _apply(
                (
                    previous.order,
                    previous.type.value,
                    previous.text,
                    previous.image,
                    previous.points,
                    previous.time_limit_seconds,
                    previous.correct_free_text,
                    previous.numeric_answer,
                    previous.usual_correct_percentage,
                    previous_options_json,
                    question_id,
                )
            )
            self._set_practice(question_id, previous.practice)
            self.reload()
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return self.get_by_id(question_id)

    def delete(self, question_id: str, _skip_reload: bool = False) -> None:
        with self._pool.connection() as conn:
            result = conn.execute(
                "delete from public.elporciento_questions where id = %s", (question_id,)
            )
            if result.rowcount == 0 and not _skip_reload:
                raise HTTPException(status_code=404, detail="Pregunta no encontrada")

        if not _skip_reload:
            self.reload()
