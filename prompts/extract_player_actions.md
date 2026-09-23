You are extracting the complete voluntary action sequence from a video clip of an online No-Limit Texas Hold'em tournament broadcast (PokerStars final table replay).

Your task: record every voluntary action taken by every player from preflop through the final street, in the order they occur.

# PLAYER CONTEXT

The following players are active in this hand. This data is authoritative:

{player_context}

Stack sizes are as of the hand setup moment. Blinds and antes have ALREADY been deducted from these figures — a BB showing 14.5 has posted its 1 BB and holds 14.5 behind it. They decrease further as players commit chips, so they are not live seat identifiers — use seat number and hole cards for identification, and treat the listed stack as each player's starting chips for this hand.

A blind-position player is all-in when the chips in front of them equal their displayed stack plus their blind. Any other player is all-in when the chips in front of them equal their displayed stack.

Use this context to:
- Attribute each action to the correct seat_position_label
- Determine whether a bet constitutes an all_in by comparing the
  committed amount to the player's remaining chips, using the rule
  stated above
- Cross-reference hole cards to confirm which player is acting
- Determine correct action order using seat numbers

# FIRST VOLUNTARY CHIP COMMITMENT CONTEXT

The first voluntary chip commitment for this hand has been identified:

{fva_context}

This action occurs at the timestamp given above. Begin recording
there — it is action_order 1 on the preflop street. Do not record
any action that precedes it, including folds. Continue recording
all subsequent actions from action_order 2 onwards.

# SCANNING INSTRUCTIONS

Begin scanning from the start of this clip. The clip begins at the new hand setup moment — both blinds are posted, all players have hole cards, no community cards are visible.

Record every voluntary action in sequence starting from action_order 1 (the first voluntary chip commitment preflop).

For each action record:
- street: preflop, flop, turn, or river
- action_order: sequential integer starting from 1, resets to 1 for each new street
- seat_position_label: position label from player context above
- action_type: fold, call, raise, bet, check, or all_in
- bet_amount: chip amount committed in BB (0.0 for fold and check)

# ACTION IDENTIFICATION

When a player acts the PokerStars UI briefly replaces their
stack size display (e.g. 20.5 BB) with the action label
(e.g. "Raise", "Call", "Fold", "Check", "All-in"). This label
is only visible for a short moment before the display reverts
to showing the updated stack size (e.g. 18.5 BB).

At 1fps this label may not be captured. Therefore use the chip
display amount as the primary signal for action_type:
- Compare the committed chip amount to the previous bet to
  determine call vs raise
- Compare the committed chip amount to the player's remaining
  chips to determine all_in. Remaining chips are the listed stack
  minus anything committed in earlier VOLUNTARY actions this hand.
  The blind is not subtracted, because it was already deducted
  from the listed stack.
- A disappearing hand of cards indicates fold
- No chips committed and action passing indicates check

# ACTION ORDER

Preflop: action starts with the highest occupied seat number and moves clockwise to the lowest. At a 9-seat table UTG (seat 9) acts first and BB (seat 1) acts last. At a 3-seat table BTN (seat 3) acts first and BB (seat 1) acts last.

Postflop: action always starts with SB (seat 2) and moves clockwise through active players to the highest occupied seat.

# WHAT COUNTS AS A VOLUNTARY ACTION

`bet_amount` is the player's TOTAL chips in front of them on that street after
the action — not the chips this action alone moved. For a seat that posted a
blind, the blind is part of that total, because it is already in front of them.

Fold and check are always 0.0, even for a seat with a blind posted.

Antes are excluded. They leave the seat and are not committed in front of it.

Displayed stacks are already net of the blind, so a blind-position player's
total is their displayed stack plus their blind when they commit everything.

Worked examples, all with an SB whose displayed stack reads 9.2 and who has
0.5 already posted:

  raises to 3.8        -> bet_amount 3.8   (3.3 more leaves the seat, 5.9 remains)
  calls a raise to 2.1 -> bet_amount 2.1   (1.6 more leaves the seat, 7.6 remains)
  moves all-in         -> bet_amount 9.7   (9.2 leaves the seat, 0 remains)
  folds                -> bet_amount 0.0   (the 0.5 posted is not recorded)

These figures are illustrative and do not correspond to any real hand. Read the
actual chip amounts from the clip; never carry a number over from this example.

The same rule applies to the BB with its 1 BB posted, and to every other seat
with nothing posted — for them the total simply equals what they committed.

Record these actions:
- Fold — player's cards disappear
- Call — matching the minimum bet (1 BB), a previous bet, or raise
- Raise — increasing a previous bet
- Bet — first chip commitment on a postflop street
- Check — no additional chips committed
- All-in — player commits their full remaining stack

# WHEN A HAND ENDS, AND WHEN CARDS KEEP COMING

Record a street ONLY if its community cards were actually dealt. Two situations
look similar in the action sequence but are opposite:

**The hand ends — record no further streets.** If every remaining player folds
to a bet, raise, or all-in, the pot is awarded immediately and NO further cards
are dealt. This is true no matter which street it happens on, and no matter how
large the final bet was. A player shoving all-in and everyone folding ends the
hand exactly as a small bet and a fold would — the all-in is irrelevant if
nobody calls.

**The hand continues without action — record the streets with empty arrays.**
If a player is all-in and at least one other player CALLS, the remaining cards
are dealt out with no further betting. Record each subsequent street with an
empty actions array.

The distinguishing question is not "was anyone all-in?" It is "did two or more
players still have live cards after the last action?" If only one player is
left, the hand is over.

Example — hand ends preflop, do NOT record flop/turn/river. The SB's displayed
stack reads 58.0 and it has 0.5 already posted, so its total is 58.5:

{
  "street_name": "preflop",
  "actions": [
    {"action_order": 1, "seat_position_label": "SB", "action_type": "all_in", "bet_amount": 58.5},
    {"action_order": 2, "seat_position_label": "BB", "action_type": "fold", "bet_amount": 0.0}
  ]
}

Example — all-in is called, cards run out, DO record the empty streets:

{
  "street_name": "turn",
  "actions": []
}

# WINNING POSITIONS

Observe which seat(s) the pot is pushed toward at the end of the hand. Record the position label for each seat that receives chips.

If the clip ends before the pot is pushed to any seat, return an empty array. Do not infer the winner from the action sequence, from who was last to bet, or from hole-card strength. An empty array is the correct answer when the award was not observed, and it is used downstream to detect a clip that did not contain the whole hand.

# EXAMPLES

Example 1 — Hand terminates preflop, 3 players:
{
  "streets": [
    {
      "street_name": "preflop",
      "actions": [
        {"action_order": 1, "seat_position_label": "BTN", "action_type": "all_in", "bet_amount": 12.5},
        {"action_order": 2, "seat_position_label": "SB", "action_type": "fold", "bet_amount": 0.0},
        {"action_order": 3, "seat_position_label": "BB", "action_type": "call", "bet_amount": 12.5}
      ]
    },
    {"street_name": "flop",  "actions": []},
    {"street_name": "turn",  "actions": []},
    {"street_name": "river",  "actions": []}
  ],
  "winning_positions": ["BTN"]
}

Example 2 — Hand terminates on flop, 2 players:
{
  "streets": [
    {
      "street_name": "preflop",
      "actions": [
        {"action_order": 1, "seat_position_label": "BTN", "action_type": "raise", "bet_amount": 2.5},
        {"action_order": 2, "seat_position_label": "BB", "action_type": "call", "bet_amount": 2.5}
      ]
    },
    {
      "street_name": "flop",
      "actions": [
        {"action_order": 1, "seat_position_label": "BB", "action_type": "check", "bet_amount": 0.0},
        {"action_order": 2, "seat_position_label": "BTN", "action_type": "bet", "bet_amount": 3.0},
        {"action_order": 3, "seat_position_label": "BB", "action_type": "fold", "bet_amount": 0.0}
      ]
    }
  ],
  "winning_positions": ["BTN"]
}

Example 3 — Hand terminates on turn, 4 players:
{
  "streets": [
    {
      "street_name": "preflop",
      "actions": [
        {"action_order": 1, "seat_position_label": "CO", "action_type": "raise", "bet_amount": 3.0},
        {"action_order": 2, "seat_position_label": "BTN", "action_type": "fold", "bet_amount": 0.0},
        {"action_order": 3, "seat_position_label": "SB", "action_type": "fold", "bet_amount": 0.0},
        {"action_order": 4, "seat_position_label": "BB", "action_type": "call", "bet_amount": 3.0}
      ]
    },
    {
      "street_name": "flop",
      "actions": [
        {"action_order": 1, "seat_position_label": "BB", "action_type": "check", "bet_amount": 0.0},
        {"action_order": 2, "seat_position_label": "CO", "action_type": "bet", "bet_amount": 4.0},
        {"action_order": 3, "seat_position_label": "BB", "action_type": "call", "bet_amount": 4.0}
      ]
    },
    {
      "street_name": "turn",
      
      "actions": [
        {"action_order": 1, "seat_position_label": "BB", "action_type": "all_in", "bet_amount": 18.5},
        {"action_order": 2, "seat_position_label": "CO", "action_type": "call", "bet_amount": 18.5}
      ]
    },
    {"street_name": "river",  "actions": []}
  ],
  "winning_positions": ["BB"]
}

Example 4 — Hand reaches river, 2 players:
{
  "streets": [
    {
      "street_name": "preflop",
      "actions": [
        {"action_order": 1, "seat_position_label": "SB", "action_type": "raise", "bet_amount": 3.0},
        {"action_order": 2, "seat_position_label": "BB", "action_type": "call", "bet_amount": 3.0}
      ]
    },
    {
      "street_name": "flop",
      
      "actions": [
        {"action_order": 1, "seat_position_label": "SB", "action_type": "bet", "bet_amount": 4.0},
        {"action_order": 2, "seat_position_label": "BB", "action_type": "call", "bet_amount": 4.0}
      ]
    },
    {
      "street_name": "turn",
      
      "actions": [
        {"action_order": 1, "seat_position_label": "SB", "action_type": "check", "bet_amount": 0.0},
        {"action_order": 2, "seat_position_label": "BB", "action_type": "check", "bet_amount": 0.0}
      ]
    },
    {
      "street_name": "river",
      
      "actions": [
        {"action_order": 1, "seat_position_label": "SB", "action_type": "bet", "bet_amount": 8.0},
        {"action_order": 2, "seat_position_label": "BB", "action_type": "fold", "bet_amount": 0.0}
      ]
    }
  ],
  "winning_positions": ["SB"]
}

Example 5 — Preflop all-in called, full runout:
{
  "streets": [
    {
      "street_name": "preflop",
      
      "actions": [
        {"action_order": 1, "seat_position_label": "CO", "action_type": "all_in", "bet_amount": 8.5},
        {"action_order": 2, "seat_position_label": "BTN", "action_type": "fold", "bet_amount": 0.0},
        {"action_order": 3, "seat_position_label": "SB", "action_type": "fold", "bet_amount": 0.0},
        {"action_order": 4, "seat_position_label": "BB", "action_type": "call", "bet_amount": 8.5}
      ]
    },
    {"street_name": "flop",  "actions": []},
    {"street_name": "turn",  "actions": []},
    {"street_name": "river",  "actions": []}
  ],
  "winning_positions": ["CO"]
}

Example 6 — Flop all-in called, turn and river run out:
{
  "streets": [
    {
      "street_name": "preflop",
      "actions": [
        {"action_order": 1, "seat_position_label": "BTN", "action_type": "raise", "bet_amount": 2.5},
        {"action_order": 2, "seat_position_label": "BB", "action_type": "call", "bet_amount": 2.5}
      ]
    },
    {
      "street_name": "flop",
      "actions": [
        {"action_order": 1, "seat_position_label": "BB", "action_type": "all_in", "bet_amount": 14.3},
        {"action_order": 2, "seat_position_label": "BTN", "action_type": "call", "bet_amount": 14.3}
      ]
    },
    {"street_name": "turn",  "actions": []},
    {"street_name": "river",  "actions": []}
  ],
  "winning_positions": ["BB"]
}

Example 7 — Turn all-in called, river runs out:
{
  "streets": [
    {
      "street_name": "preflop",
      "actions": [
        {"action_order": 1, "seat_position_label": "SB", "action_type": "raise", "bet_amount": 3.0},
        {"action_order": 2, "seat_position_label": "BB", "action_type": "call", "bet_amount": 3.0}
      ]
    },
    {
      "street_name": "flop",
      "actions": [
        {"action_order": 1, "seat_position_label": "SB", "action_type": "bet", "bet_amount": 4.0},
        {"action_order": 2, "seat_position_label": "BB", "action_type": "call", "bet_amount": 4.0}
      ]
    },
    {
      "street_name": "turn",
      "actions": [
        {"action_order": 1, "seat_position_label": "SB", "action_type": "all_in", "bet_amount": 22.7},
        {"action_order": 2, "seat_position_label": "BB", "action_type": "call", "bet_amount": 22.7}
      ]
    },
    {"street_name": "river",  "actions": []}
  ],
  "winning_positions": ["SB"]
}

Example 8 — 3-handed, one player all-in preflop, two players compete on subsequent
streets. The SB's displayed stack reads 6.0 and it has 0.5 already posted, so its
total is 6.5:
{
  "streets": [
    {
      "street_name": "preflop",
      "actions": [
        {"action_order": 1, "seat_position_label": "BTN", "action_type": "raise", "bet_amount": 3.0},
        {"action_order": 2, "seat_position_label": "SB", "action_type": "all_in", "bet_amount": 6.5},
        {"action_order": 3, "seat_position_label": "BB", "action_type": "call", "bet_amount": 6.5},
        {"action_order": 4, "seat_position_label": "BTN", "action_type": "call", "bet_amount": 6.5}
      ]
    },
    {
      "street_name": "flop",
      "actions": [
        {"action_order": 1, "seat_position_label": "BB", "action_type": "check", "bet_amount": 0.0},
        {"action_order": 2, "seat_position_label": "BTN", "action_type": "bet", "bet_amount": 8.0},
        {"action_order": 3, "seat_position_label": "BB", "action_type": "call", "bet_amount": 8.0}
      ]
    },
    {
      "street_name": "turn",      
      "actions": [
        {"action_order": 1, "seat_position_label": "BB", "action_type": "check", "bet_amount": 0.0},
        {"action_order": 2, "seat_position_label": "BTN", "action_type": "check", "bet_amount": 0.0}
      ]
    },
    {
      "street_name": "river",
      "actions": [
        {"action_order": 1, "seat_position_label": "BB", "action_type": "bet", "bet_amount": 12.0},
        {"action_order": 2, "seat_position_label": "BTN", "action_type": "fold", "bet_amount": 0.0}
      ]
    }
  ],
  "winning_positions": ["BB", "SB"]
}

Example 9 — Uncalled all-in ends the hand, 5 players. No cards are dealt,
so no further streets are recorded. The SB's displayed stack reads 58.0 and it
has 0.5 already posted, so its total is 58.5:
{
  "streets": [
    {
      "street_name": "preflop",
      "actions": [
        {"action_order": 1, "seat_position_label": "SB", "action_type": "all_in", "bet_amount": 58.5},
        {"action_order": 2, "seat_position_label": "BB", "action_type": "fold", "bet_amount": 0.0}
      ]
    }
  ],
  "winning_positions": ["SB"]
}

# OUTPUT FORMAT

Produce a single JSON object. No code fences, no preamble.

{
  "streets": [
    {
      "street_name": "<preflop | flop | turn | river>",
      "actions": [
        {
          "action_order": <integer, resets to 1 each street>,
          "seat_position_label": "<position label>",
          "action_type": "<fold | call | raise | bet | check | all_in>",
          "bet_amount": <BB amount, 0.0 for fold and check>
        }
      ]
    }
  ],
  "winning_positions": ["<position label(s) of winner(s)>"]
}

# WHAT NOT TO DO

- Do not record blind posts or antes as voluntary actions
- Do not use player names — positions are the sole identifier
- Do not skip streets with no voluntary actions — record them with empty actions arrays
- Do not rely solely on the action label — use chip display amount as primary signal
- Do not fabricate actions you did not directly observe
- Do not wrap output in code fences
- Do not return "limp" as an action_type — a 1 BB commitment is a call
- Do not infer winning_positions from the action sequence — return an empty array if you did not observe the pot being awarded
- Do not record a street whose cards were never dealt — if everyone folded to a
  bet or all-in, the hand ended there
- Do not record streets after a hand ends just because a player was all-in — an
  uncalled all-in ends the hand
   
Now extract the complete voluntary action sequence from this video clip.