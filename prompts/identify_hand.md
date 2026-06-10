You are scanning a video clip of an online No-Limit Texas Hold'em tournament broadcast (PokerStars final table replay) and identifying every new hand setup within the clip.

Your task: find every moment in this clip where a new hand has been fully set up and is ready for voluntary action. Return all such timestamps.

It is critical that you identify EVERY new hand setup in this clip without exception. Do not skip any. A missed hand setup cannot be recovered downstream — the entire extraction pipeline depends on complete detection. Scan the entire clip carefully from start to finish before returning your response.

# THE NEW-HAND-START STATE

A frame represents a valid new hand setup ONLY if ALL of the following conditions are simultaneously true:

## CONDITION 1: All players have hole cards
Every seated player who is dealt into the hand has hole cards visible at full brightness (not dimmed, not grayed out).

## CONDITION 2: ZERO community cards on the felt
The center of the table must be completely empty. No flop, turn, or river cards are visible.

## CONDITION 3: BOTH blinds posted and only forced contributions visible
You must be able to see ALL of the following simultaneously:
- EXACTLY 0.5 BB committed in front of one player (the SB)
- EXACTLY 1 BB committed in front of another player (the BB)
- The ante contribution if antes are in play
- NO other chips committed in front of any player beyond forced blinds and antes

## CONDITION 4: Pot size consistent with forced contributions only
The pot displayed in the center of the table must be 3 BB or less.
This reflects only forced contributions — blinds and antes.
If the pot exceeds 3 BB, voluntary action has occurred and this is NOT a valid setup moment.

# REJECTION CHECKLIST

Before accepting a moment as a valid new hand setup, verify the following. ANY YES answer means REJECT:

1. Are there any community cards visible in the middle of the table? → REJECT
2. Is the pot size larger than 3 BB? → REJECT

Only when BOTH checks return NO is this a valid new-hand-setup moment.

# WHAT A VALID NEW-HAND-SETUP LOOKS LIKE

- Every active player has hole cards face-up and bright
- The center of the table is empty (no community cards)
- BOTH the SB (0.5 BB) and BB (1 BB) chips are visible in front of their respective players
- The pot displays a small value (typically 1.5-3 BB)

# TIMESTAMP FORMAT

Return timestamps in absolute broadcast time:
- Use HH:MM:SS for timestamps at or beyond 1 hour (e.g., "01:23:26")
- Use MM:SS for timestamps under 1 hour (e.g., "23:26")

The clip you receive is a slice of a longer broadcast. Return absolute positions within the full broadcast, not positions within the clip.

Each hand setup should appear exactly once. Do not return multiple timestamps for the same hand.

# OUTPUT FORMAT

Produce a single JSON object. No code fences, no preamble.

{
  "hand_setups": [
    {
      "timestamp": "<MM:SS or HH:MM:SS in absolute broadcast time>",
      "pot_size_bb": <pot size in BB>,
      "community_cards_visible": <integer — must be 0>,
      "both_blinds_posted": <true or false>
    }
  ]
}

A valid new hand setup MUST satisfy ALL of the following:
- community_cards_visible = 0
- both_blinds_posted = true
- pot_size_bb <= 3

If any entry violates these conditions, you have made an error — remove it from the output before returning.

If no valid new hand setups are found in this clip, return:
{
  "hand_setups": []
}

# WHAT NOT TO DO

- Do not return timestamps where any community cards are visible
- Do not return timestamps where the pot size exceeds 3 BB
- Do not return multiple timestamps for the same hand
- Do not use the chat log for any identification
- Do not return timestamps relative to the clip — always use absolute broadcast time
- Do not wrap output in code fences

Now identify all valid new hand setups in this video clip.
