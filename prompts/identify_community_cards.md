You are identifying when a specific street begins in a video clip of a PokerStars No-Limit Texas Hold'em tournament broadcast.

Your task: find the timestamp when the {street_name} cards first appear stationary on the felt.

# STREET VISUAL REFERENCE

Three reference images are provided alongside this video showing exactly what the flop, turn, and river display looks like in this broadcast:

- Reference image — flop: shows 3 community cards face-up and stationary in a horizontal row in the center of the table
- Reference image — turn: shows 4 community cards face-up and stationary — the turn card appears to the right of the 3 flop cards
- Reference image — river: shows 5 community cards face-up and stationary — the river card appears to the right of the turn card

When scanning the video, find the moment that visually matches the {street_name} reference image — all community cards for that street fully revealed and stationary on the felt. Record that exact moment as the timestamp.

# WHAT TO LOOK FOR

- Flop: the moment 3 cards are face-up and stationary in the center
- Turn: the moment a 4th card appears face-up and stationary to the right of the flop cards
- River: the moment a 5th card appears face-up and stationary to the right of the turn card

# TIMESTAMP FORMAT

Return the timestamp in absolute broadcast time:
- Use HH:MM:SS for timestamps at or beyond 1 hour (e.g., "01:23:26")
- Use MM:SS for timestamps under 1 hour (e.g., "23:26")

Return whole seconds only. Do not return fractional seconds (e.g. "23:26.5") — the format is MM:SS or HH:MM:SS with no decimal component.

# OUTPUT FORMAT

Produce a single JSON object. No code fences, no preamble.

If the {street_name} IS found:
{
  "found": true,
  "timestamp": "<MM:SS or HH:MM:SS>"
}

If the {street_name} is NOT found in this clip:
{
  "found": false,
  "timestamp": null
}

# WHAT NOT TO DO

- Do not record the timestamp before the cards are stationary on the felt
- Do not record the timestamp during the dealing animation
- Do not record the timestamp of the last action on the previous street
- Do not fabricate a timestamp you did not directly observe
- Do not wrap output in code fences
- Do not return a fractional timestamp — whole seconds only

Now scan this clip and find when the {street_name} cards appear.