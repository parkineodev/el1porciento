from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

from .db import get_pool
from .models import GameSession, PlayerStatus
from .question_store import QuestionStore

QUALIFIED = 10


def _result_dict(result) -> dict:
    return result if isinstance(result, dict) else result.model_dump()


def _question_order(question_store: QuestionStore, question_id: str, fallback: int) -> Tuple[int, str]:
    try:
        question = question_store.get_by_id(question_id)
    except HTTPException:
        return fallback, ""
    return question.order, question.text


def build_rounds(game: GameSession, question_store: QuestionStore) -> List[dict]:
    """Por cada pregunta corregida: quién se eliminó y quién se plantó
    después de ella, en el orden de las preguntas."""
    rounds = []
    for idx, (question_id, result) in enumerate(game.question_results.items()):
        order, text = _question_order(question_store, question_id, 1000 + idx)
        cashed_out = [
            p.name
            for p in game.players.values()
            if p.status == PlayerStatus.CASHED_OUT and p.cashed_out_at_question_id == question_id
        ]
        rounds.append(
            {
                "order": order,
                "text": text,
                "eliminated": list(_result_dict(result).get("players_wrong_names") or []),
                "cashed_out": cashed_out,
            }
        )
    rounds.sort(key=lambda r: r["order"])
    return rounds


def _preboda_ranking(game_id: str) -> Optional[Tuple[float, Dict[str, Tuple[str, float]]]]:
    """Ranking general de la web de la boda sin contar esta partida de El 1%:
    mismas reglas que getOverallRanking() allí (juegos terminados, o de El 1%
    en estrategia/en vivo, con su peso). Devuelve (peso de esta partida,
    {id de jugador: (nombre, puntos)}) o None si la partida no está enlazada
    a un juego de la web."""
    with get_pool().connection() as conn:
        linked = conn.execute(
            "select id, weight from public.competition_games where elporciento_game_id = %s limit 1",
            (game_id,),
        ).fetchone()
        if not linked:
            return None
        competition_game_id, weight = linked

        players = conn.execute(
            "select id::text, display_name from public.competition_players"
        ).fetchall()
        totals = conn.execute(
            """
            select s.player_id::text, coalesce(sum(s.points * g.weight), 0)
            from public.competition_scores s
            join public.competition_games g on g.id = s.game_id
            where g.id <> %s
              and (g.status = 'finished'
                   or (g.kind = 'elporciento' and g.status in ('live', 'strategy')))
            group by s.player_id
            """,
            (competition_game_id,),
        ).fetchall()

    points_by_player = {player_id: float(total) for player_id, total in totals}
    ranking = {
        player_id: (name, points_by_player.get(player_id, 0.0)) for player_id, name in players
    }
    return float(weight), ranking


def build_recount(game: GameSession, question_store: QuestionStore) -> dict:
    """Puntos del ranking general de cada jugador en cada paso del recuento:
    antes de El 1%, tras cada pregunta y el resultado final."""
    snapshots = list(game.score_history)
    final_scores = {pid: p.score for pid, p in game.players.items()}

    steps: List[Tuple[str, Dict[str, float]]] = [("Antes de El 1%", {})]
    for idx, snap in enumerate(snapshots):
        order, _ = _question_order(question_store, snap.question_id, idx + 1)
        steps.append((f"Pregunta {order}", snap.scores))
    if not snapshots or snapshots[-1].scores != final_scores:
        steps.append(("Resultado final", final_scores))

    preboda = _preboda_ranking(game.id)
    if preboda:
        weight, ranking = preboda
        el1p_by_ref = {p.external_ref: pid for pid, p in game.players.items() if p.external_ref}
        people = [
            (player_id, name, base, el1p_by_ref.get(player_id))
            for player_id, (name, base) in ranking.items()
        ]
    else:
        # Partida suelta, sin juego en la web de la boda: solo sus puntos.
        weight = 1.0
        people = [(pid, p.name, 0.0, pid) for pid, p in game.players.items()]

    players_out = []
    for person_id, name, base, el1p_id in people:
        points = [
            round(base + (scores.get(el1p_id, 0.0) if el1p_id else 0.0) * weight, 2)
            for _, scores in steps
        ]
        players_out.append({"id": person_id, "name": name, "points": points})

    return {
        "steps": [label for label, _ in steps],
        "players": players_out,
        "qualified": QUALIFIED,
        "linked": preboda is not None,
    }
