# Changelog

All notable changes to DevStream are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Timed workflows now start from a Cloudflare cron, and hourly jobs that GitHub's scheduler skipped get restarted.
- The site says when Likes were last counted, not when it was built.
- Releases carry the provenance bundle, so they verify offline.
- The README shows the OpenSSF Best Practices badge.

### Changed

- Each entry stores its repo ID, so a rename can't be squatted and Trending stays honest across renames.
- The README and community checks say exactly what's missing.
- A submission the bot can't decide gets `needs-review` instead of failing.
- Old and already-reviewed reports expire, and un-hiding an entry is remembered.
- `flags.json` and the trust cache get pruned, so the stats files stay small.

### Fixed

- The bots ride out flaky GitHub responses, rate limits and failed writes instead of stopping, and `entries.json` saves atomically.
- The description filter handles emoji sequences, `C#` and `F#`, `?!`, `.git.git` and `###`.
- A standard license with a restriction tacked on is rejected.
- The weekly health check waits out a rate limit, and dates its notices in US Central.
- The site serves a real `robots.txt`, `sitemap.xml` and 404 page, and internal links redirect when the trailing slash is missing.

### Security

- The Cloudflare secrets live in a production environment that other branches can't reach.

## [0.1.0] - 2026-11-06

### Added

- Submit a repo through an issue form. A bot checks the account and the repo, then opens a PR that adds passing repos automatically.
- Quality bar for submissions: at least 30 days old, a push in the last 90 days, an OSI license (including ones GitHub can't detect), a real README, a description and 2 of 7 best-practice extras.
- Account trust checks: accounts must be at least 30 days old, with some public activity, and can submit up to 3 repos a week.
- A Discussion for every entry. A 👍 on it is a Like, counted once per account after trust filtering, with burst detection for sock puppets.
- A weekly health check that warns owners of quiet repos, then archives or removes dead ones.
- A "Report an entry" form, with automatic hiding for spam and malware reports from established accounts.
- The leaderboard site at https://devstream.jamesconway.dev, with categories, Top, Trending and New.
- A live `top10.svg` image and a `badge.json` shields endpoint.
- Logo, animated README header and social preview.
- README, contributing guide, governance, support and issue templates.
