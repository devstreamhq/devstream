# Contributing to DevStream

## Submit a repo

Use the [Submit a repo](https://github.com/devstreamhq/devstream/issues/new?template=submit-repo.yml) form. Please don't open a pull request that edits `data/entries.json`: the bot owns that file, and it only adds entries that come through the form.

When you submit, the bot checks two things.

Your account has to be at least 30 days old, with some public activity. You can submit up to 3 repos a week.

The repo has to:

- be at least 30 days old, with a push in the last 90 days;
- have an OSI-approved license (Learning & Resources also takes CC0-1.0, CC-BY-4.0 and CC-BY-SA-4.0), a real README and a description;
- have at least 2 of these 7 extras: a topic, a CONTRIBUTING guide, a code of conduct, an issue or pull request template, a SECURITY policy, GitHub Actions workflows, or a release.

If it passes, the bot opens a PR and the repo lands on its own. If it doesn't, the bot comments on your issue with the reasons. Fix them and submit again whenever you're ready.

## Report an entry

Found spam, malware, a dead repo or something in the wrong category? Use the [Report an entry](https://github.com/devstreamhq/devstream/issues/new?template=report-entry.yml) form. Every report goes into the review queue.

## Contribute code

The bots are Python 3.12 with the standard library only. The site is Astro, in `site/`.

1. Fork the repo and create a branch from `main`.
2. Make your change, with tests.
3. Run the checks:

   ```sh
   python3 -m unittest discover -s scripts -t .
   npm --prefix site test
   npx prettier --check .
   ```

   If prettier complains, `npx prettier --write <file>` fixes the formatting.

4. Open a pull request against `main`.

### Commit messages and PR titles

We use [Conventional Commits](https://www.conventionalcommits.org/), for example `fix(submit): reject repos with no README`. Keep the subject to 72 characters or fewer. PRs are squash-merged and the PR title becomes the commit subject, so the title has to follow the same format.

### Review

Every pull request needs an approving review before it can merge, and the PR checks have to pass: `validate`, `test`, `lint`, `site` and the three CodeQL `analyze` jobs. If you're planning something big, open an issue or start a thread in [Ideas](https://github.com/devstreamhq/devstream/discussions/categories/ideas) first, so we can agree on the shape before you write the code.

## Questions

Ask in [Discussions Q&A](https://github.com/devstreamhq/devstream/discussions/categories/q-a). See [SUPPORT.md](SUPPORT.md) for where everything else goes.

Everything you contribute is released under [CC0](LICENSE).
