-- Snapshot of the podcasts table (show list). Shows were added straight
-- into the DB, so this is the copy in git. Safe to re-run: skips any
-- rss_url already present.
--   psql -d <db> -f seeds/podcasts.sql

INSERT INTO podcasts (name, rss_url, active, category)
SELECT 'Hockey Gambling Podcast', 'https://feeds.simplecast.com/caDpJU1D', true, 'betting'
WHERE NOT EXISTS (SELECT 1 FROM podcasts WHERE rss_url = 'https://feeds.simplecast.com/caDpJU1D');

INSERT INTO podcasts (name, rss_url, active, category)
SELECT 'PuckTime', 'https://www.spreaker.com/show/6653697/episodes/feed', true, 'betting'
WHERE NOT EXISTS (SELECT 1 FROM podcasts WHERE rss_url = 'https://www.spreaker.com/show/6653697/episodes/feed');

INSERT INTO podcasts (name, rss_url, active, category)
SELECT 'Sick Hockey Picks', 'https://feeds.megaphone.fm/SICMED8934387209', true, 'betting'
WHERE NOT EXISTS (SELECT 1 FROM podcasts WHERE rss_url = 'https://feeds.megaphone.fm/SICMED8934387209');

INSERT INTO podcasts (name, rss_url, active, category)
SELECT 'The Ice Guys', 'https://feeds.simplecast.com/j21MjKd6', true, 'betting'
WHERE NOT EXISTS (SELECT 1 FROM podcasts WHERE rss_url = 'https://feeds.simplecast.com/j21MjKd6');

INSERT INTO podcasts (name, rss_url, active, category)
SELECT 'The Daily Juice - BettingPros', 'https://rss.pdrl.fm/49ade8/www.omnycontent.com/d/playlist/e73c998e-6e60-432f-8610-ae210140c5b1/58472ddd-be56-4002-8937-b2d70144b8d5/cddd4ab4-85f4-4769-9ca3-b2d70144b8ef/podcast.rss', true, 'betting'
WHERE NOT EXISTS (SELECT 1 FROM podcasts WHERE rss_url = 'https://rss.pdrl.fm/49ade8/www.omnycontent.com/d/playlist/e73c998e-6e60-432f-8610-ae210140c5b1/58472ddd-be56-4002-8937-b2d70144b8d5/cddd4ab4-85f4-4769-9ca3-b2d70144b8ef/podcast.rss');

INSERT INTO podcasts (name, rss_url, active, category)
SELECT 'Between the Lines - Hockey Betting Advice', 'https://anchor.fm/s/fb810ca8/podcast/rss', true, 'betting'
WHERE NOT EXISTS (SELECT 1 FROM podcasts WHERE rss_url = 'https://anchor.fm/s/fb810ca8/podcast/rss');

INSERT INTO podcasts (name, rss_url, active, category)
SELECT 'NHL Run Down Show', 'https://www.podserve.fm/feeds/nhl-run-down-show-tAzcU1mZ.rss', true, 'betting'
WHERE NOT EXISTS (SELECT 1 FROM podcasts WHERE rss_url = 'https://www.podserve.fm/feeds/nhl-run-down-show-tAzcU1mZ.rss');

INSERT INTO podcasts (name, rss_url, active, category)
SELECT 'Sports Parlay Picks', 'https://anchor.fm/s/4b12f64c/podcast/rss', true, 'betting'
WHERE NOT EXISTS (SELECT 1 FROM podcasts WHERE rss_url = 'https://anchor.fm/s/4b12f64c/podcast/rss');

INSERT INTO podcasts (name, rss_url, active, category)
SELECT 'Play Me or Fade Me', 'https://feeds.megaphone.fm/playmeorfademe', true, 'betting'
WHERE NOT EXISTS (SELECT 1 FROM podcasts WHERE rss_url = 'https://feeds.megaphone.fm/playmeorfademe');

INSERT INTO podcasts (name, rss_url, active, category)
SELECT 'The Early Edge (CBS Sports)', 'https://rss.amperwave.net/v2/feed/audacynetwork/earlyedge', true, 'betting'
WHERE NOT EXISTS (SELECT 1 FROM podcasts WHERE rss_url = 'https://rss.amperwave.net/v2/feed/audacynetwork/earlyedge');

INSERT INTO podcasts (name, rss_url, active, category)
SELECT 'The Hammer Daily', 'https://feeds.megaphone.fm/HAMMR9076370074', true, 'betting'
WHERE NOT EXISTS (SELECT 1 FROM podcasts WHERE rss_url = 'https://feeds.megaphone.fm/HAMMR9076370074');

INSERT INTO podcasts (name, rss_url, active, category)
SELECT 'The Propcast', 'https://feeds.simplecast.com/mc2hMWfs', true, 'betting'
WHERE NOT EXISTS (SELECT 1 FROM podcasts WHERE rss_url = 'https://feeds.simplecast.com/mc2hMWfs');

