You are extracting static state data from a single HIGH resolution frame of an online No-Limit Texas Hold'em tournament broadcast. The frame shows a PokerStars final table replay at the moment of a new hand setup — all players have hole cards, both blinds are posted, all screen names are visible, and no voluntary action has occurred yet.

This is the cleanest possible table state. Use it to establish:
- Total number of seats at the table
- Position label for each seat
- Stack size for each seat

# OBSERVATION RULES

1. Observe only what is visible in this frame. Do not infer, calculate, or fabricate any value.

2. If a value is not clearly readable, return null. Missing data is acceptable; fabricated data is not.

3. All numeric values (stacks, pot size) are denominated in big blinds (BB). Read these values as displayed — do not round or normalize.

# TOTAL SEAT COUNT

Count every seat with a player visible at the table. This is the total_seat_count. In a PokerStars final table replay, eliminated players' seats are physically removed — every visible seat is an active player.

# POSITION ASSIGNMENT

This is the sole step where position labels are assigned for this hand. Use the following procedure carefully — do not use poker knowledge to shortcut this process.

## Step 1: Find the BB anchor

The Big Blind is always identifiable by the "1 BB" chip display committed in front of a player. This is seat 1 and your fixed anchor.

Do NOT rely on the dealer button — it can appear off-center or ambiguous.
Do NOT use poker knowledge to assume which seat is which position.

## Step 2: Count counter-clockwise from BB

Starting from BB (seat 1) and moving counter-clockwise around the table, assign a seat number to each occupied seat. Each occupied seat increments the count by 1.

## Step 3: Map seat number to position label

Use this exact mapping — no exceptions:

Seat 1 = BB
Seat 2 = SB
Seat 3 = BTN
Seat 4 = CO
Seat 5 = HJ
Seat 6 = LJ
Seat 7 = UTG+2
Seat 8 = UTG+1
Seat 9 = UTG

## Step 4: Verification

CHECK A: Each position label appears at most once in your output.
CHECK B: SB and BB are both present in the output.
CHECK C: The number of players in your output equals total_seat_count.
CHECK D: The SB player has 0.5 BB committed in front of them.
CHECK E: The BB player has 1 BB committed in front of them.

If any check fails, recount your seat assignments starting from the BB anchor.

# STACK SIZES

Read the stack size displayed under each player's name at their seat
in big blinds (BB). This is a numeric value typically between 1 and
300 BB for a tournament final table.

ONLY read the BB-denominated stack display. Do not read:
- Dollar amounts or bounty values (e.g. $586.12, $1,043.93) — these
  appear as currency values with a $ prefix and are NOT stack sizes
- Chip counts (large integers without BB denomination)
- Any value that appears to be a currency amount

If the BB stack size is not clearly visible at a seat — for example
because the player is disconnected and their stack display is replaced
by a status indicator — return null for that player's stack_size.
Do not substitute any other visible numeric value.

# OUTPUT FORMAT

Produce a single JSON object. No code fences, no preamble.

{
  "hand_setup": {
    "total_seat_count": <number of seats visible at the table>,
    "players": [
      {
        "seat_position_label": "<position label from mapping above>",
        "stack_size": <stack size in BB>
      }
    ],
    "pot_size_bb": <pot size in BB or null if not visible>
  }
}


# WHAT NOT TO DO

- Do not use the dealer button for position assignment — use the BB chip display only
- Do not use poker knowledge to assign position labels — use the seat mapping only
- Do not include player_name — positions are the sole identifier
- Do not include hole_cards — those are captured separately at a later step
- Do not include seat_number — that is derived by Python from the position label
- Do not read pot chips or committed chips as stack sizes
- Do not normalize or round displayed values — capture exact displayed values
- Do not include code fences around the JSON
