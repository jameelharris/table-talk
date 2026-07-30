You are identifying a specific moment within a video window of an online No-Limit Texas Hold'em tournament broadcast (PokerStars final table replay).

Your task: find the first voluntary chip commitment within this hand and, if observable, identify when the second action occurs. Either or both may be absent — return null for any value you cannot directly observe.

# WINDOW CONTEXT

This window is {available_seconds} seconds long.

# PLAYER CONTEXT

The following positions, seat numbers, and stack sizes were established from a HIGH resolution frame at the new hand setup moment. This data is authoritative:

{player_context}

Use these exact position labels and seat numbers. Do not derive or count positions independently — match each acting seat to one of the entries above using the seat-identification procedure below.

# SEAT IDENTIFICATION (do this BEFORE identifying any action)

Map each physical seat in the frame to its player-context entry BEFORE looking for actions. Do this positionally, NOT by stack size — two players can have identical stacks, so stack size is unreliable for telling seats apart.

Anchor on the small blind:

1. Find the SB — the seat with exactly 0.5 BB posted in front of it. This is the most reliable anchor: 0.5 BB is ONLY ever the small blind's forced post. No player ever voluntarily commits exactly 0.5 BB, so this seat is unambiguous even when other seats show matching chip amounts. In the player context, this is the seat labeled "SB".

2. From the SB, map every other seat using the context's seat numbers and the physical layout. The BB sits immediately to the SB's left (clockwise). Continue around the table to place every seat listed in the context by its seat number.

Do NOT use stack size to identify or distinguish seats. Use the SB's 0.5 BB post as the anchor and the seat layout to place the rest.

# DEFINITION OF FIRST VOLUNTARY CHIP COMMITMENT

The first voluntary chip commitment occurs when a seat has chips committed beyond their forced contribution. This is your sole trigger — do not rely on action labels (Raise, Call, All-in).

Forced contributions to ignore:
- SB: exactly 0.5 BB in front of them — forced, ignore
- BB: exactly 1 BB in front of them — forced, ignore
- Ante chips in front of any seat — forced, ignore

A voluntary chip commitment occurs when:
- A seat has chips committed beyond their forced contribution
- This includes SB completing to 1 BB or raising beyond 1 BB
- This includes BB raising beyond 1 BB
- This includes any other seat committing any chips voluntarily

# IDENTIFYING THE FIRST VOLUNTARY ACTOR (scan in action order)

Preflop, action proceeds in a fixed order, and you MUST scan the seats in that same order so the first voluntary commitment you find is genuinely the first to occur — not merely the most visually prominent or the largest.

Scan order:

- Preflop action begins at the seat first to act and proceeds toward the blinds.
- In the player context's seat numbering, the seat first to act is the HIGHEST seat number present in the context, and action proceeds in DESCENDING seat-number order down to the BB (seat 1), which acts last.
- Therefore: start at the highest-numbered seat listed in the player context and scan downward through decreasing seat numbers.

For each seat in that descending order, check whether it has committed chips beyond its forced contribution. STOP at the FIRST seat that has — that seat is the first voluntary actor. Do not continue scanning once you have found it.

CRITICAL: Because you scan in action order, a later or larger commitment (for example a raise from a lower-numbered seat) must NEVER be reported as the first voluntary actor if an earlier seat (higher seat number) already committed voluntarily. A 1 BB limp from the first-to-act seat IS the first voluntary commitment even if a later seat raised to a larger amount — the limp came first.

# DEFINITION OF SECOND ACTION

The second action is the next observable change at the table after the first voluntary chip commitment:
- Any other seat folding (cards are mucked)
- Any other seat calling (chips appear in front of them)
- Any other seat raising (chips appear in front of them)
- The BB checking (no chips but action moves)

The window between first voluntary chip commitment and second action timestamps is used to extract a clean frame for hole card extraction.

# WHAT IS NOT A FIRST VOLUNTARY CHIP COMMITMENT

- Posting the small blind (exactly 0.5 BB from the SB seat) — forced
- Posting the big blind (exactly 1 BB from the BB seat) — forced. NOTE: a 1 BB commitment from any seat OTHER than the BB is a voluntary limp/call, not a forced post.
- Posting an ante — forced
- Folding — no chip commitment

# BET AMOUNT

The bet_amount is the total chips the FIRST voluntary actor has committed AT THE MOMENT of their first voluntary commitment — not a later action, not the largest bet in the hand. Read the chips in front of THAT specific seat at THAT moment, denominated in big blinds (BB).

- Read only the chips in front of the first voluntary actor's seat
- Read them at the moment of their first voluntary commitment, not later
- If the first voluntary action is a limp or call, the amount is typically 1 BB — do NOT report a later raise's larger amount
- If you find yourself about to report a bet larger than the first voluntary commitment, you have advanced too far in time; return to the first moment chips were voluntarily committed and read THAT amount
- Do not read the pot total
- Do not read another seat's chips
- Do not read the blind amount

CORRECT (BB-denominated): 2.09, 6.5, 13.6, 43.7
WRONG (chip-denominated): 1087500, 3375000, 7050000, 22700568

If bet_amount is larger than 200 you are almost certainly reading the wrong number.

# ACTION TYPE

Determine action_type from the first voluntary actor's committed amount relative to the current bet facing them at the moment they act:

- If the amount matches the largest amount already committed (1 BB preflop when no one has raised) → call
- If the amount exceeds that and is less than the player's full stack → raise
- If the amount equals the player's full remaining stack → all_in

Derive action_type from the FVA's own chip amount at the moment of their commitment — not from a later action, and not from action labels.

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
- Do not identify seats by stack size — anchor on the SB's 0.5 BB post and map the rest by seat layout
- Do not scan for the first voluntary actor out of order — scan from the highest seat number down to the BB
- Do not report a later or larger commitment as the first voluntary actor if an earlier (higher-numbered) seat already committed voluntarily
- Do not read bet_amount from a later action — read the first voluntary actor's chips at the moment of their commitment
- Do not derive position labels or seat numbers independently — use only values from player context
- Do not use action labels as the trigger — use chip display only
- Do not ignore SB or BB as potential first voluntary actors — they can act voluntarily beyond their forced contribution
- Do not treat SB completing to 1 BB as a forced contribution — it is voluntary
- Do not treat a 1 BB commitment from a non-BB seat as a forced post — it is a voluntary limp/call
- Do not return "limp" as an action_type — a 1 BB commitment is a call
- Do not return chip-denominated bet amounts — always BB-denominated
- Do not read bet amounts from pot total or another seat's chips
- Do not return timestamps relative to the window — always absolute broadcast time
- Do not wrap output in code fences

Now identify the first voluntary chip commitment and second action in this video window.