You are identifying a specific moment within a video window of an online No-Limit Texas Hold'em tournament broadcast (PokerStars final table replay).

Your task: find the first voluntary chip commitment within this hand and, if observable, identify when the second action occurs. Either or both may be absent — return null for any value you cannot directly observe.

# WINDOW CONTEXT

This window is {available_seconds} seconds long.

# PLAYER CONTEXT

The following positions, seat numbers, and stack sizes were established from a HIGH resolution frame at the new hand setup moment. This data is authoritative:

{player_context}

Use these exact position labels and seat numbers when identifying the first voluntary chip commitment. Do not derive or count positions independently — match the acting seat to one of the entries above.

# DEFINITION OF FIRST VOLUNTARY CHIP COMMITMENT

The first voluntary chip commitment occurs when any seat has chips committed beyond their forced contribution. This is your sole trigger — do not rely on action labels (Raise, Call, All-in).

Forced contributions to ignore:
- SB: exactly 0.5 BB in front of them — forced, ignore
- BB: exactly 1 BB in front of them — forced, ignore
- Ante chips in front of any seat — forced, ignore

A voluntary chip commitment occurs when:
- ANY seat has chips committed beyond their forced contribution
- This includes SB completing to 1 BB or raising beyond 1 BB
- This includes BB raising beyond 1 BB
- This includes any other seat committing any chips voluntarily

Watch for any seat where the chip display exceeds their forced contribution amount.

# DEFINITION OF SECOND ACTION

The second action is the next observable change at the table after the first voluntary chip commitment:
- Any other seat folding (cards are mucked)
- Any other seat calling (chips appear in front of them)
- Any other seat raising (chips appear in front of them)
- The BB checking (no chips but action moves)

The window between first voluntary chip commitment and second action timestamps is used to extract a clean frame for hole card extraction.

# WHAT IS NOT A FIRST VOLUNTARY CHIP COMMITMENT

- Posting the small blind (exactly 0.5 BB in front of SB) — forced
- Posting the big blind (exactly 1 BB in front of BB) — forced
- Posting an ante — forced
- Folding — no chip commitment

# POSITION IDENTIFICATION

When chips appear beyond forced contributions, identify which position from the player context above committed those chips. Match the seat visually to the position label and seat number — use the stack size as a cross-reference if needed.

# BET AMOUNT

The bet_amount is the total chips committed by the first voluntary chip commitment actor at the moment of their action, denominated in big blinds (BB). Read directly from the chip display in front of their seat.

- Read only the chips in front of the acting seat
- Do not read the pot total
- Do not read another seat's chips
- Do not read the blind amount

CORRECT (BB-denominated): 2.09, 6.5, 13.6, 43.7
WRONG (chip-denominated): 1087500, 3375000, 7050000, 22700568

If bet_amount is larger than 200 you are almost certainly reading the wrong number.

# ACTION TYPE

Determine action_type from the chip amount committed relative to the current bet facing the actor:

- If the chip amount matches the largest amount already committed by another seat (1 BB preflop when no one has raised) → call
- If the chip amount exceeds that amount and is less than the player's full stack → raise
- If the chip amount equals the player's full remaining stack → all_in

Do not rely on action labels — derive action_type from chip amount alone.

# UNCONTESTED HANDS

If all players fold without any voluntary chip commitment — BB wins by default — return found: false with reason: uncontested.

# TIMESTAMP FORMAT

Return timestamps in absolute broadcast time:
- Use HH:MM:SS for timestamps at or beyond 1 hour (e.g., "01:23:26")
- Use MM:SS for timestamps under 1 hour (e.g., "23:26")

The window you receive is a slice of a longer broadcast. Return absolute positions within the full broadcast, not positions within the window.

# OUTPUT FORMAT

Produce a single JSON object. No code fences, no preamble.

If a first voluntary chip commitment IS found and second action IS found:
{
  "found": true,
  "timestamp": "<MM:SS or HH:MM:SS>",
  "second_action_timestamp": "<MM:SS or HH:MM:SS>",
  "seat_position_label": "<position label from player context above>",
  "action_type": "<call, raise, or all_in>",
  "bet_amount": <total chips committed by this player in BB>
}

If hand is uncontested:
{
  "found": false,
  "reason": "uncontested",
  "timestamp": null,
  "second_action_timestamp": null,
  "seat_position_label": null,
  "action_type": null,
  "bet_amount": null
}

If first voluntary chip commitment IS NOT found:
{
  "found": false,
  "reason": "no_first_voluntary_commitment_found",
  "timestamp": null,
  "second_action_timestamp": null,
  "seat_position_label": null,
  "action_type": null,
  "bet_amount": null
}

If first voluntary chip commitment IS found but second action IS NOT found:
{
  "found": true,
  "reason": "no_second_action_found",
  "timestamp": "<MM:SS or HH:MM:SS>",
  "second_action_timestamp": null,
  "seat_position_label": "<position label from player context above>",
  "action_type": "<call, raise, or all_in>",
  "bet_amount": <total chips committed by this player in BB>
}

# WHAT NOT TO DO

- Do not fabricate any timestamp you did not directly observe — return null instead
- Do not return a second_action_timestamp if the window ends before a second action occurs
- Do not estimate or infer a second_action_timestamp from context
- Do not derive position labels or seat numbers independently — use only values from player context
- Do not use action labels as the trigger — use chip display only
- Do not ignore SB or BB as potential first voluntary chip commitment actors — they can act voluntarily beyond their forced contribution
- Do not treat SB completing to 1 BB as a forced contribution — it is voluntary
- Do not return "limp" as an action_type — a 1 BB commitment is a call
- Do not return chip-denominated bet amounts — always BB-denominated
- Do not read bet amounts from pot total or another seat's chips
- Do not return timestamps relative to the window — always absolute broadcast time
- Do not wrap output in code fences

Now identify the first voluntary chip commitment and second action in this video window.