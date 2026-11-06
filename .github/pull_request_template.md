## What and why

<!-- What does this change, and what problem does it solve? Link the issue if there is one. -->

## Checks

- [ ] The PR title follows [Conventional Commits](https://www.conventionalcommits.org/)
- [ ] `python3 -m unittest discover -s scripts -t .` passes
- [ ] `npm --prefix site test` passes (if you touched `site/`)
- [ ] `npx prettier --check .` passes
- [ ] This doesn't edit `data/entries.json` by hand (submissions go through the form)
