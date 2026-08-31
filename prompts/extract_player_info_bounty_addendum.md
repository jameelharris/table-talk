## Additional fields for this extraction

Add the following inside the existing "hand_setup" object:

- On each object inside "hand_setup.players", a "bounty" field alongside
  "stack_size": that same seat's bounty badge, shown just below the player's
  avatar as a currency amount. Report it as a number, without currency symbol or
  thousands separators. The bounty you report for a seat must be the badge
  belonging to the player whose stack you reported for that seat. Null if not
  legible.

This is an addition. Every field you were already producing must still be
produced, with the same names, the same nesting and the same accuracy. If the new
field is not legible, report null for it rather than degrading or omitting an
existing field.
