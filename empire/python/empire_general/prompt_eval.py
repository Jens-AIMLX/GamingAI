"""Prompt-Eval: Claude analysiert einen Taktik-/Aufstellungs-/Strategie-Prompt und schlaegt eine bessere Fassung vor.

Bewertet wird, ob der Feldherr den Text so ausfuehren KANN (Aufgaben, Reflexe, Grenzen der Script-API) und ob er
eindeutig ist. Antwort in zwei Teilen: Analyse und ueberarbeitete Zeilen.

Diff-Management: Zeilen, die seit der letzten uebernommenen Ueberarbeitung unveraendert sind ("geschuetzt"), werden
nicht angefasst - das Modell sieht sie nur als Kontext und ueberarbeitet ausschliesslich neue/geaenderte Zeilen.
Das Zusammensetzen (merge) passiert hier im Code, geschuetzte Zeilen koennen also gar nicht veraendert werden.

    from empire_general.prompt_eval import review_lines
    res = review_lines("orders_player", text, baseline_lines, api_key=..., model="claude-sonnet-5-5")
    res.analysis, res.merged_text, res.changes
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .general import SYSTEM_PROMPT

KINDS = {
    "orders_player": "binding ORDERS for the PLAYER'S army (the side of the human who writes them)",
    "orders_enemy": "binding ORDERS for the ENEMY army (the opponent of the human player)",
    "strategy": "general STRATEGY ADVICE for both sides (only lines starting with '- ' reach the general; "
                "it ranks below the binding orders)",
    "deploy": "the DEPLOYMENT PROMPT: how the army is to be placed before the battle starts. The general uses only "
              "move tasks there; the system itself already keeps all target points within about 150 m of the area "
              "the army occupies - the player does not need to write any distance limit",
}


class WireReview(BaseModel):
    analysis: str = Field(description="Part 1 - analysis in German, plain text without Markdown: what works, what "
                                      "the general cannot execute and why, ambiguities, contradictions (also with "
                                      "the protected lines), missing triggers. Max. ~250 words")
    revised: list[str] = Field(description="Exactly one entry per OPEN line, in the same order: the improved line "
                                           "(starting with '- '), or the unchanged line if it is already good, or "
                                           "an empty string to delete it")
    added: list[str] = Field(description="New lines ('- ...') only if something the player clearly intends is "
                                         "missing; usually []")


EVAL_SYSTEM = """\
You review prompts that a human player writes for an AI general in Total War: EMPIRE. The general receives the \
prompt and plans every 5-15 seconds; a subordinate executes the plans with a fixed set of tasks and reflexes. \
Your job: judge whether the general can carry the text out as the player intends, and improve the OPEN lines.

This is exactly what the general is told about its command (its own system prompt):
-----
{general}
-----

Rules for your revision:
- You only revise the OPEN lines. PROTECTED lines were already reviewed and accepted by the player: never repeat, \
rephrase or move them; they are shown only as context. If an open line contradicts a protected line, keep the \
player's newer open line and point the conflict out in the analysis.
- Keep the player's intent, style of play, priorities and all numbers they wrote. Never invent new restrictions, \
limits or priorities the player did not state (no new distance limits, no "this rule overrides everything", no \
extra conditions). Only make vague wording concrete where the player's meaning is clear.
- Executability: every instruction must map onto the tasks (move, attack, halt, withdraw), shapes, waypoints, \
reflexes (engage_within_m, keep_range_m, near/routed/arrived/after reflexes, fire_at_will, skirmish) or targets \
(unit names/ids, roles like general, artillery, cavalry) the general has. If something cannot be ordered (e.g. \
ammunition type, formation type), say so in the analysis and rephrase it to the closest executable behaviour.
- Staying in place is halt, not move: if the player wants units to stay where they stand ("bleibt stehen", \
"hält die Position", "bleibt unverändert in Linie"), write halt (with engage_within_m if they may strike back). \
Never write "move ... an der aktuellen Position" or "move to the current position": a move re-forms the group \
around a new target point and makes the units leave their places.
- If an open line is already good, return it unchanged.
- When a CURRENT BATTLEFIELD is given (live from the running battle), the player's lines refer to that army: check \
each line against it - which real units are meant (names as listed, e.g. 'Infanterie 2', 'Artillerie 1'), whether \
there are enough of them, whether the space fits their frontages. Where the player's meaning is clear, make the \
line concrete in the way the general places groups: relative to the front, to the rear or to a named unit - \
behind / in front (metres), left / right (metres, gap), as seen by the army facing the enemy. Never write world \
coordinates. Never change who is meant: if the player names a unit kind (e.g. "the heavy artillery"), you may \
list the matching units, but do not add or drop units. Terrain (hills, crests, woods, rivers) is not known to the \
general - only the ground height under each unit; if a line relies on terrain, keep it, say in the analysis that \
the general can only approximate it from those heights, and add the closest executable placement in the same line.
- Without a CURRENT BATTLEFIELD, judge only the wording and do not name units that may not exist.
Write in German, as plain text without Markdown (no ** or #). Lines start with '- '.\
"""


@dataclass
class ReviewResult:
    analysis: str
    merged_text: str
    changes: list[tuple[str, str]] = field(default_factory=list)   # (alt, neu); alt "" = neu hinzugefuegt, neu "" = entfernt
    protected: int = 0
    open: int = 0


def split_lines(text: str) -> list[str]:
    return [l.rstrip() for l in text.strip().splitlines()]


def classify(text: str, baseline: list[str] | None) -> tuple[list[str], list[int], list[int]]:
    """Zeilen des Textes -> (Zeilen, Indizes geschuetzt, Indizes offen). Geschuetzt = wortgleich in der Fassung,
    die bei der letzten Ueberarbeitung uebernommen wurde. Leere Zeilen sind weder noch."""
    lines = split_lines(text)
    base = {l.strip() for l in (baseline or []) if l.strip()}
    prot = [i for i, l in enumerate(lines) if l.strip() and l.strip() in base]
    opn = [i for i, l in enumerate(lines) if l.strip() and l.strip() not in base]
    return lines, prot, opn


def review_lines(kind: str, text: str, baseline: list[str] | None, *, api_key: str | None = None,
                 model: str = "claude-sonnet-5-5", effort: str = "medium", situation: str = "") -> ReviewResult:
    import anthropic

    if kind not in KINDS:
        raise ValueError(f"unbekannte Art {kind!r}")
    lines, prot, opn = classify(text, baseline)
    if not opn:
        return ReviewResult(analysis="Keine neuen oder geänderten Zeilen seit der letzten Überarbeitung – nichts zu prüfen.",
                            merged_text="\n".join(lines), protected=len(prot), open=0)
    protected_txt = "\n".join(lines[i] for i in prot) or "(none)"
    open_txt = "\n".join(f"{n + 1}. {lines[i]}" for n, i in enumerate(opn))
    battlefield = (f"CURRENT BATTLEFIELD (live from the running battle - the lines refer to this army):\n-----\n"
                   f"{situation.strip()}\n-----\n\n") if situation.strip() else \
        "CURRENT BATTLEFIELD: not available (no battle running) - judge only the wording.\n\n"
    user = (f"Type of prompt: {KINDS[kind]}.\n\n" + battlefield +
            f"PROTECTED lines (already reviewed - do not change, context only):\n-----\n{protected_txt}\n-----\n\n"
            f"OPEN lines to review ({len(opn)}; return exactly {len(opn)} entries in 'revised', same order):\n"
            f"-----\n{open_txt}\n-----\n\nGive the analysis and the revised open lines.")
    client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=120.0)
    try:
        resp = client.beta.messages.parse(
            model=model,
            max_tokens=16000,
            system=[{"type": "text", "text": EVAL_SYSTEM.format(general=SYSTEM_PROMPT),
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=WireReview,
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
    w = resp.parsed_output
    if w is None:
        raise RuntimeError(f"keine gültige Antwort (stop_reason {resp.stop_reason})")
    return merge(lines, opn, w.revised, w.added, w.analysis, len(prot))


def merge(lines: list[str], opn: list[int], revised: list[str], added: list[str], analysis: str,
          n_protected: int) -> ReviewResult:
    """Ueberarbeitete Zeilen an die Stellen der offenen Zeilen setzen; geschuetzte und leere Zeilen bleiben, wie sie
    sind. Liefert das Modell eine falsche Anzahl, bleiben die nicht zuordenbaren offenen Zeilen unveraendert."""
    out = list(lines)
    changes: list[tuple[str, str]] = []
    note = ""
    if len(revised) != len(opn):
        note = (f"\n\nHinweis: Das Modell lieferte {len(revised)} statt {len(opn)} überarbeitete Zeilen – "
                "nicht zuordenbare Zeilen bleiben unverändert.")
    for n, i in enumerate(opn):
        if n >= len(revised):
            break
        new = revised[n].rstrip()
        if new.strip() != lines[i].strip():
            changes.append((lines[i], new))
            out[i] = new
    for a in added:
        if a.strip():
            changes.append(("", a.rstrip()))
            out.append(a.rstrip())
    # vom Modell geloeschte offene Zeilen (leerer String) entfernen, echte Leerzeilen der Vorlage behalten
    final = [x for k, x in enumerate(out) if not (k in opn and x.strip() == "")]
    return ReviewResult(analysis=analysis.strip() + note, merged_text="\n".join(final).strip(),
                        changes=changes, protected=n_protected, open=len(opn))
