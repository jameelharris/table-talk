You are reading the tournament results panel from a single HIGH resolution frame of an online No-Limit Texas Hold'em tournament broadcast (PokerStars final table replay). The panel sits in the lower-right of the screen and lists the tournament's prize ladder: one row per finishing position, showing the rank and the prize paid for it. It is static for the whole broadcast.

Your task: report whether the panel is fully visible, whether it carries a Bounty column, and the value printed on every row of the ladder.

# OBSERVATION RULES

1. Observe only what is visible in this frame. Do not infer, calculate, or fabricate any value.

2. If a value is not clearly readable, return null. Missing data is acceptable; fabricated data is not.

3. Read digits exactly as printed, including cents. Do not round, normalize, or tidy a displayed value.

# PANEL VISIBILITY

Set panel_visible to true only if the results panel is fully and legibly present in this frame.

Set panel_visible to FALSE if any of the following holds:
- The panel is absent from the frame.
- The panel is occluded by an overlay, a graphic, or a title card.
- The panel is scrolled such that rank 1 is not shown.
- The panel is cut off at any edge of the frame, even slightly.
- The panel's full header row is not visible.

The last two matter more than they look. A panel clipped on its right-hand edge hides the Bounty column, and a Bounty column you cannot see is indistinguishable from one that does not exist. Reporting such a frame as visible would silently classify a bounty tournament as a non-bounty one. When the panel is not fully framed, say so.

If panel_visible is false, return an empty rows array and null for currency_symbol and has_bounty_column.

# BOUNTY COLUMN

Knockout tournaments print an additional Bounty column in the results panel, alongside the prize column. Non-knockout tournaments do not.

Set has_bounty_column to true if that column is present in the panel's header row, false if it is not. Judge this from the panel's own header — not from anything else in the frame, and not from the tournament's name.

If has_bounty_column is false, return null for bounty on every row.

# LADDER ROWS

Produce one object per rank printed in the panel, in ascending rank order, starting at rank 1.

Read each row independently:
- rank: the finishing position printed on that row.
- payout: the prize amount printed on that row, as a number.
- bounty: the bounty amount printed on that row, as a number, or null if there is no Bounty column.

Numbers only. Do not include the currency symbol and do not include thousands separators — write 1359.37, not "$1,359.37".

Some broadcasts prefix a payout with an asterisk. When a payout is printed with a leading asterisk, set payout_marked to true and report payout without the asterisk. When it is not, set payout_marked to false. Report the asterisk as you find it; do not reason about what it means.

Read each row's figures from that row. Do not derive a value from the pattern of the rows above or below it — a prize ladder looks regular, and a plausible value reconstructed from that regularity is indistinguishable from a real one once it is stored. If a row's figure is not legible, return null for it.

# CURRENCY SYMBOL

Report the currency symbol printed on the panel's amounts, for example "$". A single symbol covers the whole panel.

# OUTPUT FORMAT

Produce a single JSON object. No code fences, no preamble.

If the panel IS fully visible:

{
  "panel_visible": true,
  "has_bounty_column": <true or false>,
  "currency_symbol": "<currency symbol printed on the panel>",
  "rows": [
    {
      "rank": <finishing position as printed>,
      "payout": <prize amount as a number, or null if not legible>,
      "payout_marked": <true if the printed payout carries a leading asterisk, else false>,
      "bounty": <bounty amount as a number, null if no Bounty column or not legible>
    }
  ]
}

If the panel is NOT fully visible:

{
  "panel_visible": false,
  "has_bounty_column": null,
  "currency_symbol": null,
  "rows": []
}

# WHAT NOT TO DO

- Do not report panel_visible true for a panel that is clipped at any edge — a hidden Bounty column reads as an absent one
- Do not determine has_bounty_column from the tournament's name, from seat badges, or from anything but the panel's header row
- Do not include a currency symbol or thousands separators in payout or bounty — numbers only
- Do not infer a payout or bounty from the pattern of the other rows — return null for a figure you cannot read
- Do not include the asterisk in the payout value — report it via payout_marked
- Do not interpret what the asterisk means, or adjust a marked payout because of it
- Do not report a bounty on any row when has_bounty_column is false
- Do not omit ranks that are printed, or invent ranks that are not
- Do not round or normalize displayed values — capture exact displayed values
- Do not wrap output in code fences

Now read the tournament results panel from this frame.
