# NHL Betting Podcast Extraction Prompt (Claude Code CLI)

Invoked by worker.py as `claude -p "<this file>"` with a roster/team context
block and the transcript on stdin.

---

You are analyzing an NHL betting podcast transcript. Extract every specific
NHL bet the analysts discuss: player props, team bets, and game bets.

This is for informational comparison only - nothing extracted here is used
for any wagering or monetary decision. Record what the hosts actually said,
not whether their read was right.

## What to extract

Each item is ONE bet on ONE subject. Three kinds of subject:

- `subject_type: "player"` - a player prop. `bet_type` is one of:
  "anytime_goal", "first_goal", "goals", "shots_on_goal", "points",
  "assists", "power_play_points", "blocked_shots", "saves" (goalie),
  "player_other" (a real player prop that fits none of these).
- `subject_type: "team"` - a bet on one team. `bet_type` is one of:
  "moneyline", "puck_line", "regulation_win", "team_total", "team_other".
- `subject_type: "game"` - a bet on a whole game. `bet_type` is one of:
  "game_total", "first_period", "both_teams_score", "game_other".
- Season-long bets (Stanley Cup, division, awards, season win totals):
  use `subject_type` "team" (or "player" for awards), `bet_type`
  "futures", `timeframe` "futures".

If one statement covers several subjects (e.g. "I like Draisaitl and
McDavid to go over on shots"), emit a SEPARATE item per subject, sharing the
same `quote_text`.

## Fields

- `subject_type`: as above.
- `raw_mention`: for a player or team, the name exactly as said in the
  transcript (don't normalize - matching happens later). For a game bet,
  the two teams as said, e.g. "Oilers vs Flames".
- `team_hint`: for a player, the team abbreviation (EDM, TOR, ...) if the
  transcript or the roster context makes it clear; for a team bet, the
  abbreviation of the team being backed; for a game bet, the first team.
  null if unknown.
- `opponent_hint`: abbreviation of the opposing team if mentioned or clear
  from the discussion, else null.
- `bet_type`: as above.
- `side`: "over" or "under" if the bet is a line the analyst is talking
  about as over/under (shots, points, saves, totals), else null.
- `lean`: "favorable" if the analyst is recommending / likes the bet as
  described (including the `side` they stated); "unfavorable" if they are
  recommending AGAINST it (fading, "stay away", "I hate it"); "neutral" for
  purely descriptive mentions with no read.
- `sharp_or_public`: "sharp" or "public" ONLY if the analyst explicitly
  frames the take as professional vs recreational money (e.g. "sharps are
  all over this", "the public is hammering it"). Otherwise null - never
  infer from tone.
- `timeframe`: "tonight" (today's games), "upcoming" (a specific later
  game, e.g. tomorrow or Saturday), "past" (recapping how a previous bet or
  game turned out - NOT a pick), or "futures".
- `line_context`: JUST the number/line, short and scannable: "over 3.5",
  "-1.5", "+135", "6.5", "-110". null if none was given. Do not put
  narration here (line movement goes in `betting_relevance`).
  Bets are settled against this later, so capture the LINE whenever one is
  stated or clearly implied, and keep it separate from the odds:
    * "to record a point" / "get a point" -> side "over", line "0.5" (points)
    * "to get an assist" -> side "over", line "0.5" (assists)
    * "multi-point game" -> side "over", line "1.5" (points)
    * "3+ shots" -> side "over", line "2.5"; "over 2.5 shots" -> side "over", line "2.5"
    * puck line: keep the SIGN, "-1.5" (favorite) or "+1.5" (underdog)
  American odds like "+205" or "-160" are odds, not a line; if the only
  number given is odds, put the odds here and leave `side` null.
- `quote_text`: the actual statement, under 40 words, verbatim.
- `speaker`: the host/guest name if identifiable, else the show name.
- `timestamp_sec`: approximate timestamp from the transcript markers.
- `tags`: short lowercase strings describing the take. Prefer existing
  tags: "line_movement", "injury_impact", "matchup_based", "goalie_based",
  "back_to_back", "rest_advantage", "usage_based", "power_play",
  "narrative_fade", "correlation_play", "value_price". Invent a short new
  tag if none fits.
- `betting_relevance`: ONE sentence on the reasoning behind the take, not
  just the number.

## Prioritize

Specific bets with real reasoning, goalie matchups and starts, line
movement commentary, injury/lineup news that changes a bet, and explicit
sharp-vs-public framing.

## Skip

Generic game previews with no bet attached, standings/news chatter, banter,
sponsor reads and promo codes, other sports (NFL, NBA, MLB, college), and
anything with no identifiable bet.

## Output

Output strictly a JSON array. If nothing meets the bar, return `[]`.

CRITICAL OUTPUT RULE: Your entire response must be ONLY the JSON array. No
preamble, no explanation, no commentary about the context or transcript
quality. The first character must be [ and the last character must be ].
