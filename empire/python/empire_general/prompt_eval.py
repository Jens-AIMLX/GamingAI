"""Prompt-Eval: Claude analysiert einen Taktik-/Aufstellungs-/Strategie-Prompt und schlaegt eine bessere Fassung vor.

Bewertet wird, ob der Feldherr den Text so ausfuehren KANN (Aufgaben, Reflexe, Grenzen der Script-API) und ob er
eindeutig ist. Antwort in zwei Teilen: analysis (Befund) und suggestion (neue Fassung, Absicht des Spielers bleibt).

    from empire_general.prompt_eval import evaluate
    res = evaluate("orders_player", text, api_key=..., model="claude-sonnet-5-5")
    res.analysis, res.suggestion
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .general import SYSTEM_PROMPT

KINDS = {
    "orders_player": "binding ORDERS for the PLAYER'S army (the side of the human who writes them)",
    "orders_enemy": "binding ORDERS for the ENEMY army (the opponent of the human player)",
    "strategy": "general STRATEGY ADVICE for both sides (only lines starting with '- ' reach the general; "
                "it ranks below the binding orders)",
    "deploy": "the DEPLOYMENT PROMPT: how the army is to be placed before the battle starts (only move tasks, "
              "the deployment zone is limited and its edges are unknown; units should stay near their start area)",
}


class PromptReview(BaseModel):
    analysis: str = Field(description="Part 1 - analysis in German: what works, what the general cannot execute "
                                      "and why, ambiguities, contradictions, missing triggers or time limits. "
                                      "Short paragraphs or bullet points, max. ~250 words")
    suggestion: str = Field(description="Part 2 - the improved text in German, ready to paste: one instruction per "
                                        "line, each starting with '- '. Keep the player's intent and priorities; "
                                        "make every line executable with the general's tasks and reflexes; drop or "
                                        "rephrase what cannot be ordered and say so in the analysis")


EVAL_SYSTEM = """\
You review prompts that a human player writes for an AI general in Total War: EMPIRE. The general receives the \
prompt and plans every 5-15 seconds; a subordinate executes the plans with a fixed set of tasks and reflexes. \
Your job: judge whether the general can carry the text out as the player intends, and propose a better version.

This is exactly what the general is told about its command (its own system prompt):
-----
{general}
-----

Review criteria:
- Executability: every instruction must map onto the tasks (move, attack, halt, withdraw), shapes, waypoints, \
reflexes (engage_within_m, keep_range_m, near/routed/arrived/after reflexes, fire_at_will, skirmish) or targets \
(unit ids, roles like general, artillery, cavalry) the general has. Name instructions that cannot be ordered \
(e.g. ammunition type, formation type) and replace them with the closest executable behaviour.
- Clarity: concrete triggers instead of vague words ("when enemy infantry is within 120 m" instead of "when they \
come close"), concrete time limits in battle time, clear priorities when instructions compete.
- Consistency: contradictions between lines, and with the side the text is meant for.
- Keep the player's intent, style of play and priorities. Do not invent a different strategy.
Write both parts in German, as plain text without Markdown (no ** or #); use short headings followed by a colon \
and lines starting with '- '. The suggestion is a list of lines starting with '- '.\
"""


def evaluate(kind: str, text: str, *, api_key: str | None = None, model: str = "claude-sonnet-5-5",
             effort: str = "medium") -> PromptReview:
    import anthropic

    if kind not in KINDS:
        raise ValueError(f"unbekannte Art {kind!r}")
    client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=120.0)
    user = (f"Type of prompt: {KINDS[kind]}.\n\nThe player's text:\n-----\n{text.strip() or '(empty)'}\n-----\n\n"
            "Give the analysis and the improved version.")
    try:
        resp = client.beta.messages.parse(
            model=model,
            max_tokens=16000,
            system=[{"type": "text", "text": EVAL_SYSTEM.format(general=SYSTEM_PROMPT),
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=PromptReview,
            output_config={"effort": effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as e:
        raise RuntimeError("Anmeldung fehlgeschlagen – API-Schlüssel prüfen") from e
    except anthropic.RateLimitError as e:
        raise RuntimeError(f"Rate-Limit: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"API-Fehler {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise RuntimeError("Keine Verbindung zur Claude-API") from e
    if resp.stop_reason == "refusal":
        raise RuntimeError("Das Modell hat die Prüfung abgelehnt")
    if resp.parsed_output is None:
        raise RuntimeError(f"keine gültige Antwort (stop_reason {resp.stop_reason})")
    return resp.parsed_output
