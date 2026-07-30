You are extracting hole card information from a single HIGH resolution frame of an online No-Limit Texas Hold'em tournament broadcast (PokerStars final table replay).

This frame was captured at the moment of the first voluntary chip commitment. At this moment all eligible players have hole cards visible at their seat.

# HOLE CARD EXTRACTION PROCEDURE

Follow these steps in order:

## Step 1: Understand what is already known
The positions and stack sizes below were established from a clean earlier frame and are AUTHORITATIVE. Each eligible seat is identified by its position label and its stack size:

{hole_card_context}

Your job is NOT to re-derive who sits where — that is already determined. Your job is to read each listed seat's hole cards and attach them to the correct seat.

## Step 2: Identify each seat by its stack, then read its cards
For EACH seat in the eligible list above, locate that specific player in the frame and read their two hole cards. Identify each seat by its stack size, which is displayed under that player's name:
- Match each seat to the player whose displayed stack equals the stack size listed for that seat. The stack under a player's name is a reliable per-seat identifier.
- Read the two hole cards belonging to that same player (the cards shown at that player's seat, next to their name and stack).
- Do this for each seat independently — locate the player by stack, read their cards. Do not assume seats are in any particular visual order; find each one by its stack.

If two seats share the same stack size, disambiguate them by their neighbors: the listed seats are consecutive around the table, so a tied seat's position is fixed by the seats on either side of it (whose stacks differ).

- Read cards ONLY for the seats in the eligible list. Seats not listed are not to be read.
- If you cannot confidently match a player to their listed seat, or cannot read their cards clearly, return null for that seat. A card assigned to the wrong seat is worse than a null — when in doubt, return null.

# CARD READING

The video uses a 4-color deck. Use BOTH color AND physical shape
to identify suits:

- BLUE colored cards that contain diamond (◆) shapes are Diamonds
- RED colored cards that contain heart (♥) shapes are Hearts
- GREEN colored cards that contain clover (♣) shapes are Clubs
- BLACK colored cards that contain spade (♠) shapes are Spades

Note: Diamonds are BLUE and angular (◆). Spades are BLACK with
a rounded top and stem (♠). These are the two most commonly
confused suits — verify carefully.

When color is ambiguous — use the physical shape of the symbol
as the tiebreaker.

Card notation:
- Ranks: 2, 3, 4, 5, 6, 7, 8, 9, T, J, Q, K, A
- Suits: c (Clubs), d (Diamonds), h (Hearts), s (Spades)

# CARD UNIQUENESS
Every card in a standard deck is unique. No two players can hold
the same card. Before returning your response, verify that no card
appears more than once across all players' hole cards.

If a card is not clearly readable, return null for that player's hole_cards.

# OUTPUT FORMAT

Produce a single JSON object. No code fences, no preamble.

{
  "players": [
    {
      "seat_position_label": "<position label from eligible list above>",
      "hole_cards": [<two cards e.g. "Ah", "Kd"> or null if not readable]
    }
  ]
}

# WHAT NOT TO DO

- Do not extract hole cards for seats beyond the FVA in counter-clockwise order
- Do not include seat_number in output — it is derived by Python
- Do not assume a 2-color deck — use the 4-color suit mapping above
- Do not invent cards that are not clearly visible
- Do not include community cards — hole cards only
- Do not include player_name — positions are the sole identifier
- Do not include stack_size — carry through from existing player records
- Do not wrap output in code fences

Now extract hole cards for all eligible players from this frame.