-- NHL Betting Buzz schema. Applied by init_db.py (idempotent).
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS teams (
    abbrev TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    nickname TEXT,
    division TEXT,
    conference TEXT
);

CREATE TABLE IF NOT EXISTS team_aliases (
    id SERIAL PRIMARY KEY,
    team TEXT NOT NULL REFERENCES teams(abbrev),
    alias TEXT NOT NULL,
    UNIQUE (team, alias)
);
CREATE INDEX IF NOT EXISTS idx_team_aliases_trgm ON team_aliases USING gin (alias gin_trgm_ops);

CREATE TABLE IF NOT EXISTS players (
    id SERIAL PRIMARY KEY,
    nhl_id INTEGER UNIQUE,
    full_name TEXT NOT NULL,
    team TEXT REFERENCES teams(abbrev),
    position TEXT,
    active BOOLEAN NOT NULL DEFAULT true,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS player_aliases (
    id SERIAL PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(id),
    alias TEXT NOT NULL,
    source TEXT DEFAULT 'auto',
    UNIQUE (player_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_player_aliases_trgm ON player_aliases USING gin (alias gin_trgm_ops);

CREATE TABLE IF NOT EXISTS games (
    id SERIAL PRIMARY KEY,
    nhl_game_id BIGINT UNIQUE NOT NULL,
    season INTEGER NOT NULL,
    game_type INTEGER NOT NULL,          -- 1 preseason, 2 regular, 3 playoffs
    game_date DATE NOT NULL,             -- local (arena) date
    start_time_utc TIMESTAMPTZ NOT NULL,
    away TEXT NOT NULL REFERENCES teams(abbrev),
    home TEXT NOT NULL REFERENCES teams(abbrev),
    state TEXT,
    away_score INTEGER,
    home_score INTEGER
);
CREATE INDEX IF NOT EXISTS idx_games_date ON games (game_date);
CREATE INDEX IF NOT EXISTS idx_games_start ON games (start_time_utc);

CREATE TABLE IF NOT EXISTS podcasts (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    rss_url TEXT NOT NULL UNIQUE,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    active BOOLEAN NOT NULL DEFAULT true,
    category TEXT NOT NULL DEFAULT 'betting'
);

CREATE TABLE IF NOT EXISTS episodes (
    id SERIAL PRIMARY KEY,
    podcast_id INTEGER NOT NULL REFERENCES podcasts(id),
    guid TEXT NOT NULL,
    title TEXT,
    published_at TIMESTAMPTZ NOT NULL,
    audio_url TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    transcript TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    error_message TEXT,
    UNIQUE (podcast_id, guid)
);

-- One row per thing an analyst said about a bet. subject_type says what the
-- bet is ON: a player (props), a team (moneyline / puck line / team total),
-- or a whole game (game total, first period, both teams to score).
CREATE TABLE IF NOT EXISTS bet_quotes (
    id SERIAL PRIMARY KEY,
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    subject_type TEXT NOT NULL,          -- player | team | game
    player_id INTEGER REFERENCES players(id),
    team TEXT REFERENCES teams(abbrev),  -- the team backed / the player's team / first team of a game bet
    opponent TEXT REFERENCES teams(abbrev),
    raw_mention TEXT NOT NULL,
    match_confidence TEXT NOT NULL DEFAULT 'unreviewed',
    bet_type TEXT NOT NULL,
    lean TEXT NOT NULL DEFAULT 'neutral',
    sharp_or_public TEXT,
    timeframe TEXT,                      -- tonight | upcoming | past | futures
    line_context TEXT,
    quote_text TEXT NOT NULL,
    speaker TEXT,
    timestamp_sec INTEGER,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    betting_relevance TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (episode_id, quote_text, raw_mention)
);
CREATE INDEX IF NOT EXISTS idx_bet_quotes_player ON bet_quotes (player_id);
CREATE INDEX IF NOT EXISTS idx_bet_quotes_team ON bet_quotes (team);

CREATE TABLE IF NOT EXISTS pipeline_status (
    id INTEGER PRIMARY KEY DEFAULT 1,
    is_running BOOLEAN NOT NULL DEFAULT false,
    current_episode_id INTEGER,
    current_episode_title TEXT,
    stage TEXT,
    episodes_total_this_run INTEGER,
    episodes_done_this_run INTEGER NOT NULL DEFAULT 0,
    run_started_at TIMESTAMPTZ,
    stage_started_at TIMESTAMPTZ,
    avg_seconds_per_episode NUMERIC,
    last_finished_at TIMESTAMPTZ,
    last_poll_at TIMESTAMPTZ,
    last_poll_new_count INTEGER
);
INSERT INTO pipeline_status (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- Betting shows (all NHL-only, verified active as of 2026-10-07). Mixed-sport
-- shows would be gated by episode_filter.py so we don't transcribe non-hockey
-- episodes.
INSERT INTO podcasts (name, rss_url) VALUES
    ('Hockey Gambling Podcast', 'https://feeds.simplecast.com/caDpJU1D'),
    ('PuckTime', 'https://www.spreaker.com/show/6653697/episodes/feed'),
    ('Sick Hockey Picks', 'https://feeds.megaphone.fm/SICMED8934387209')
ON CONFLICT (rss_url) DO NOTHING;
