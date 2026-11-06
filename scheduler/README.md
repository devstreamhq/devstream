# Scheduler

GitHub Actions skips scheduled runs when it's busy, and on a quiet repo it skips a lot of them. Likes once went seven hours without a sync. This Cloudflare Worker starts the timed workflows instead, through the GitHub API, on a cron that Cloudflare keeps.

One cron trigger fires every 15 minutes. Each tick starts:

| When (UTC)     | Workflow                            |
| -------------- | ----------------------------------- |
| every tick     | `sweep.yml`                         |
| :15 every hour | `likes.yml`                         |
| :30 every hour | `on-merge.yml`                      |
| Mondays 06:45  | `rot.yml` (the weekly health check) |
| daily 09:45    | `stars.yml`                         |

`likes.yml`, `on-merge.yml` and `rot.yml` have no GitHub schedule of their own, so they never run twice. `sweep.yml` and `stars.yml` keep theirs as a fallback. If the Worker stops, the sweep still restarts a stalled Likes sync or on-merge run, and still runs an overdue health check.

The Worker has no URL. It does nothing but run on its cron.

## Token

The Worker needs one secret, `GITHUB_TOKEN`: a fine-grained personal access token with

- resource owner `devstreamhq`, repository `devstreamhq/devstream` only
- **Actions: Read and write**, nothing else
- an expiry about a year out (it's tracked with the other rotations)

## Deploy

From the repo root, with the Wrangler version the site pins:

```sh
cd scheduler
npx --prefix ../site wrangler login
npx --prefix ../site wrangler deploy
npx --prefix ../site wrangler secret put GITHUB_TOKEN
```

The last command asks you to paste the token. Run `wrangler deploy` again whenever `index.js` or `wrangler.toml` changes. The secret stays put.

To check it's working, open the Worker in the Cloudflare dashboard and look at **Cron events**, or watch the Actions tab for `workflow_dispatch` runs every 15 minutes.

## Tests

`site/tests/scheduler.test.mjs` runs with the site tests (`npm --prefix site test`).
