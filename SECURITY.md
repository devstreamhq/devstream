# Security policy

Found a way to break DevStream, or to bend its rankings? Tell us privately first. A public issue hands the trick to everyone before we can close it.

## Supported versions

We fix security problems on `main` and in the latest release. Older releases don't get patches; the fix lands in the next one.

| Version        | Supported |
| -------------- | --------- |
| `main`         | Yes       |
| Latest release | Yes       |
| Anything older | No        |

## Reporting a vulnerability

We use GitHub's private vulnerability reporting, so there's no email address to hunt for.

1. Open the [Security tab](https://github.com/devstreamhq/devstream/security) of this repo.
2. Select **Report a vulnerability**.
3. Tell us what you found, how to reproduce it and what an attacker could do with it.

Only you and the maintainers can see the report, and we'll talk it through with you in the advisory.

## What happens next

We acknowledge every report within 48 hours. Within 7 days you get our first assessment: whether we can reproduce it, how serious we think it is and what we plan to do. Disclosure is coordinated with you and happens within 90 days of your report.

We'll credit you in the advisory if you'd like.

## What's in scope

- The bot and its workflows: anything in `scripts/` or `.github/workflows/` that lets someone run code, read secrets, write to the repo or push the bot into a decision it shouldn't make.
- The site: script injection, broken escaping of entry data, or anything that turns the leaderboard against the people reading it.
- Data integrity, because ranking manipulation counts. If you can get an entry listed past the quality bar, keep one alive that should be removed, or remove someone else's, we want to hear about it.
- Likes gaming. A way to inflate Likes that slips past the burst check and the trust rule is a vulnerability, even when no code misbehaves.

## What's out of scope

The repos listed on DevStream belong to their own maintainers. If you find a hole in one of them, report it to that project through its own security policy. If a listed repo is malicious, use the [Report an entry](https://github.com/devstreamhq/devstream/issues/new?template=report-entry.yml) form and pick "Malicious". A report from an established account hides the entry while we look.

Denial of service by flooding the issue tracker, and findings that need a compromised maintainer account, are out of scope too.

## Safe harbour

Research in good faith is welcome here. If you follow this policy, we won't pursue legal action or ask anyone else to, and we'll treat your work as authorised. Good faith means you:

- test against your own accounts and test repos, not other people's entries or Likes,
- stop as soon as you've shown the problem, and don't keep or share data that isn't yours,
- give us the time above before going public.

Not sure whether something is covered? Ask in the advisory before you try it. We'd much rather answer a question than read about it later.
