# Governance

DevStream is a small project, and this page says who decides what.

## Maintainer

[@jamesconwaydev](https://github.com/jamesconwaydev) maintains DevStream. He reviews pull requests, reads reports and has the final say when there's a disagreement.

## How decisions get made

Decisions happen in the open, in [Discussions](https://github.com/devstreamhq/devstream/discussions) or in issues. If you want something changed, such as a new category or a different quality bar, start a thread in [Ideas](https://github.com/devstreamhq/devstream/discussions/categories/ideas). We talk it through there, and the outcome is written down in the thread before anything ships.

## Bots and humans

Bots handle the routine work, and they follow published rules:

- submissions are checked against the quality bar in [CONTRIBUTING.md](CONTRIBUTING.md#submit-a-repo);
- Likes are counted once per account, after trust filters and burst detection;
- a weekly health check warns the owner of a repo that has gone quiet, then archives or removes it.

When a case doesn't fit the rules, a human looks at it. Anything a bot can't decide gets the `needs-review` label, and a maintainer picks it up.

## Moderation

Anyone can [report an entry](https://github.com/devstreamhq/devstream/issues/new?template=report-entry.yml), and every report about a listed repo reaches a maintainer. Some reports also act straight away. When 3 established accounts report the same entry as spam, or one established account reports it as malicious, the entry is hidden while a maintainer reviews it. A report that an entry is dead queues a health check.

Submissions whose description matches a term in `scripts/blocklist.txt` (SEO, casino, scam and similar spam) are rejected. Everything else is moderated by hand.

## Blocks

The maintainer can block an account from the `devstreamhq` organization. A blocked account can't submit repos, its Likes don't count and its reports aren't acted on automatically. Blocks are for spam, sock-puppet voting and abuse.

All participation is covered by the [Code of Conduct](https://github.com/devstreamhq/.github/blob/main/CODE_OF_CONDUCT.md).
