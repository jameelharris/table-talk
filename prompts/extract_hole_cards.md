You are extracting hole card information from a single HIGH resolution frame of an online No-Limit Texas Hold'em tournament broadcast (PokerStars final table replay).

This frame was captured at the moment of the first voluntary chip commitment. At this moment all eligible players have hole cards visible at their seat.

# FIRST VOLUNTARY ACTOR

{fva_context}

The FVA is the last seat you will extract hole cards from.

# HOLE CARD EXTRACTION PROCEDURE

Follow these steps in order:

## Step 1: Find BB (seat 1)
Locate the player with "1 BB" committed in front of them. This is seat 1 (BB) and your anchor.

## Step 2: Extract hole cards counter-clockwise from BB through FVA
Starting from BB (seat 1), move counter-clockwise around the table extracting hole cards for each seat in the following list. Stop when you reach the FVA:

{hole_card_context}

Each entry shows the seat number and position label. Match each seat number by counting counter-clockwise from BB (seat 1).

## Step 3: Stop at FVA
Do not extract hole cards for any seat beyond the FVA in counter-clockwise order. Players in those seats already folded before the FVA acted — their card areas are empty.

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