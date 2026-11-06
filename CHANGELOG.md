# Changelog

All notable changes to DevStream are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Submit a repo through an issue form. A bot checks the account and the repo, then opens a PR that adds passing repos automatically.
- Quality bar for submissions: at least 30 days old, a push in the last 90 days, an OSI license (including ones GitHub can't detect), a real README, a description and 2 of 7 best-practice extras.
- Account trust checks: accounts must be more than 30 days old with real activity, and can submit up to 3 repos a week.
- A Discussion for every entry. A 👍 on it is a Like, counted once per account after trust filtering, with burst detection for sock puppets.
- A weekly health check that warns owners of quiet repos, then archives or removes dead ones.
- A "Report an entry" form, with automatic hiding for spam and malware reports from established accounts.
- The leaderboard site at https://devstream.jamesconway.dev, with categories, Top, Trending and New.
- A live `top10.svg` image and a `badge.json` shields endpoint.
- Logo, animated README header and social preview.
- README, contributing guide, governance, support and issue templates.
