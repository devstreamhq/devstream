"""Idempotent GitHub setup for DevStream: labels, issue types and fields, the
roadmap project, milestones, the backlog, and repo and org settings.

python3 scripts/admin/setup_github.py [--rewrite]

Needs an authenticated gh with org admin rights and the project scope. Every
step looks before it writes, so a second run changes nothing. On existing
issues it only fills what's empty, so edits made on GitHub survive a re-run;
--rewrite also resets backlog titles' bodies to the text below.
"""

import json
import subprocess
import sys
import urllib.parse

ORG = "devstreamhq"
REPO = f"{ORG}/devstream"
OWNER_LOGIN = "jamesconwaydev"
FIELDS_API = "X-GitHub-Api-Version: 2026-03-10"
PROJECT_TITLE = "DevStream Roadmap"

changes: list[str] = []
skipped: list[str] = []
notes: list[str] = []
# A feature our plan doesn't include isn't a failure; the step starts working if the org upgrades.
PLAN_GATE = "Upgrade to GitHub Team"


class GhError(Exception):
    pass


def changed(what: str) -> None:
    changes.append(what)
    print(f"  changed: {what}")


def gh(*args, body=None):
    r = subprocess.run(
        ["gh", *args], input=None if body is None else json.dumps(body), capture_output=True, text=True
    )
    if r.returncode:
        raise GhError(f"gh {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()}")
    return json.loads(r.stdout) if r.stdout.strip() else None


def api(method, path, body=None, header=None):
    args = ["api", "-X", method, path] + (["-H", header] if header else [])
    return gh(*args, "--input", "-", body=body) if body is not None else gh(*args)


def paginate(path, header=None):
    pages = gh("api", path, "--paginate", "--slurp", *(["-H", header] if header else []))
    return [x for page in pages for x in page]


def graphql(query, **variables):
    out = gh("api", "graphql", "--input", "-", body={"query": query, "variables": variables})
    if out.get("errors"):
        raise GhError(str(out["errors"]))
    return out["data"]


def optional(name, fn):
    """Run a step the plan or API may not allow; record it instead of failing the run."""
    try:
        fn()
    except GhError as e:
        if PLAN_GATE in str(e):
            notes.append(f"{name}: not on this plan ({e})")
            print(f"  not on this plan: {name}")
            return
        skipped.append(f"{name}: {e}")
        print(f"  skipped {name}: {e}")


# ---------------------------------------------------------------- labels

WORKFLOW = "Used by the bots. Don't rename it."
# Workflow label colours and descriptions match setup_repo.sh, so the two scripts never fight.
LABELS = {
    "submission": ("0e8a16", "Repo submission"),
    "accepted": ("1d76db", "Submission accepted"),
    "rejected": ("b60205", "Submission rejected"),
    "needs-review": ("fbca04", "Needs a maintainer"),
    "report": ("d93f0b", "Entry reported"),
    "superseded": ("c5def5", "Reported as replaced by another repo"),
    "priority: high": ("b8001f", "Fix this before the next release"),
    "priority: medium": ("e8833a", "Next in line once the high ones are done"),
    "priority: low": ("f3d36b", "Worth doing on a quiet afternoon"),
    "area: bot": ("bfd4f2", "The Python bots that take submissions, count Likes and check health"),
    "area: site": ("bfd4f2", "devstream.jamesconway.dev and how it's built"),
    "area: data": ("bfd4f2", "entries.json and the files on the stats branch"),
    "area: docs": ("bfd4f2", "The README, the wiki and every other page we publish"),
    "area: ci": ("bfd4f2", "Workflows, action pins, releases and deploys"),
    "area: security": ("bfd4f2", "Anything someone could use to game the list or hurt its users"),
    "good first issue": ("7057ff", "Small, well scoped, and a good place to start"),
    "help wanted": ("008672", "We'd love a hand with this one"),
    "bug": ("d73a4a", "Something's broken"),
    "enhancement": ("a2eeef", "Something new, or something better"),
    "documentation": ("0075ca", "Words that need writing or fixing"),
    "dependencies": ("0366d6", "A dependency bump, usually from Dependabot"),
}


# The only labels this script ever deletes. Anything else not in LABELS is someone's, so it stays.
DEFAULT_DELETE = {"duplicate", "invalid", "question", "wontfix"}


def label_plan(have: dict, want: dict, in_use: set):
    """Return (create, update, delete, kept) label names. Colours compare case-blind."""
    create = [n for n in want if n not in have]
    update = [n for n in want if n in have and (have[n][0].lower(), have[n][1]) != want[n]]
    doomed = [n for n in have if n in DEFAULT_DELETE]
    return create, update, [n for n in doomed if n not in in_use], [n for n in doomed if n in in_use]


def labels():
    print("labels")
    have = {x["name"]: (x["color"], x["description"] or "") for x in paginate(f"repos/{REPO}/labels?per_page=100")}
    # Any issue or PR, open or closed, still wearing a label keeps it; deleting would strip it from them.
    in_use = {n for n in have if n in DEFAULT_DELETE
              and api("GET", f"repos/{REPO}/issues?state=all&per_page=1&labels={urllib.parse.quote(n)}")}
    create, update, delete, kept = label_plan(have, LABELS, in_use)
    for n in create:
        api("POST", f"repos/{REPO}/labels", {"name": n, "color": LABELS[n][0], "description": LABELS[n][1]})
        changed(f"label {n} created")
    for n in update:
        api("PATCH", f"repos/{REPO}/labels/{urllib.parse.quote(n, safe='')}", {"color": LABELS[n][0], "description": LABELS[n][1]})
        changed(f"label {n} updated")
    for n in delete:
        api("DELETE", f"repos/{REPO}/labels/{urllib.parse.quote(n, safe='')}")
        changed(f"label {n} deleted")
    for n in kept:
        print(f"  kept {n}: an issue or PR uses it")


# ----------------------------------------------------------- issue types

TYPES = {
    "Bug": ("red", "Something that worked, or should have, and doesn't"),
    "Feature": ("blue", "Something new you'll be able to do"),
    "Task": ("yellow", "A piece of work that just needs doing"),
    "Epic": ("purple", "A bigger goal, split into sub-issues"),
    "Docs": ("green", "Words to write or fix"),
    "Security": ("orange", "A weakness someone could use against the list or the people on it"),
}


def issue_types():
    print("issue types")
    have = {t["name"]: t for t in api("GET", f"orgs/{ORG}/issue-types")}
    for name, (color, desc) in TYPES.items():
        body = {"name": name, "is_enabled": True, "color": color, "description": desc}
        t = have.get(name)
        if t is None:
            api("POST", f"orgs/{ORG}/issue-types", body)
            changed(f"issue type {name} created")
        elif (t["color"], t["description"], t["is_enabled"]) != (color, desc, True):
            api("PUT", f"orgs/{ORG}/issue-types/{t['id']}", body)
            changed(f"issue type {name} updated")


# ---------------------------------------------------------- issue fields

ISSUE_FIELDS = {
    "Priority": ("How soon this needs doing", [("High", "red"), ("Medium", "yellow"), ("Low", "green")], {}),
    "Area": (
        "Which part of DevStream this touches",
        [("Bot", "blue"), ("Site", "purple"), ("Data", "green"), ("Docs", "yellow"), ("CI", "gray"), ("Security", "red")],
        {},
    ),
    # GitHub's default Effort field ships as High/Medium/Low; rename those in place.
    "Effort": (
        "Roughly how big: S is an afternoon, M a couple of days, L a week or more",
        [("S", "green"), ("M", "yellow"), ("L", "red")],
        {"S": "Low", "M": "Medium", "L": "High"},
    ),
}


def field_options(have: list, want: list, alias: dict | None = None) -> list:
    """Options for a PATCH: existing ids kept (by name, or by alias for a rename), order by `want`."""
    ids = {o["name"]: o["id"] for o in have}
    alias = alias or {}
    out = []
    for i, (name, color) in enumerate(want, 1):
        oid = ids.get(name, ids.get(alias.get(name)))
        out.append(({"id": oid} if oid is not None else {}) | {"name": name, "color": color, "priority": i})
    return out


def issue_fields():
    print("issue fields")
    have = {f["name"]: f for f in api("GET", f"orgs/{ORG}/issue-fields", header=FIELDS_API)}
    for name, (desc, opts, alias) in ISSUE_FIELDS.items():
        f = have.get(name)
        if f is None:
            body = {"name": name, "description": desc, "data_type": "single_select", "visibility": "all",
                    "options": field_options([], opts)}
            api("POST", f"orgs/{ORG}/issue-fields", body, header=FIELDS_API)
            changed(f"issue field {name} created")
            continue
        now = [(o["name"], o["color"]) for o in sorted(f.get("options") or [], key=lambda o: o["priority"])]
        if (now, f["description"], f["visibility"]) != (opts, desc, "all"):
            body = {"description": desc, "visibility": "all", "options": field_options(f.get("options") or [], opts, alias)}
            api("PATCH", f"orgs/{ORG}/issue-fields/{f['id']}", body, header=FIELDS_API)
            changed(f"issue field {name} updated")


def issue_field_ids() -> dict:
    """{field name: (field node id, {option name: option node id})}; dates have no options."""
    q = """query Fields($org: String!) { organization(login: $org) { issueFields(first: 50) { nodes {
      ... on IssueFieldSingleSelect { id name options { id name } }
      ... on IssueFieldDate { id name } } } } }"""
    nodes = graphql(q, org=ORG)["organization"]["issueFields"]["nodes"]
    return {n["name"]: (n["id"], {o["name"]: o["id"] for o in n.get("options", [])}) for n in nodes if n}


# --------------------------------------------------------------- project

PROJECT_FIELDS = {
    "Priority": ["High", "Medium", "Low"],
    "Size": ["S", "M", "L"],
    "Area": ["Bot", "Site", "Data", "Docs", "CI", "Security"],
    "Target date": None,
}
VIEWS = [("Backlog", "TABLE_LAYOUT", ""), ("Board", "BOARD_LAYOUT", ""),
         ("Roadmap", "ROADMAP_LAYOUT", ""), ("Bugs", "TABLE_LAYOUT", "type:Bug")]
PROJECT_BLURB = "What we're building next for DevStream, and what's already done."


def project_state(number):
    q = """query Project($org: String!, $n: Int!) { organization(login: $org) { projectV2(number: $n) {
      id public shortDescription
      repositories(first: 20) { nodes { nameWithOwner } }
      views(first: 30) { nodes { id name layout filter } }
      fields(first: 50) { nodes {
        ... on ProjectV2SingleSelectField { id name options { id name } }
        ... on ProjectV2Field { id name } } } } } }"""
    return graphql(q, org=ORG, n=number)["organization"]["projectV2"]


def project() -> dict:
    print("project")
    found = [p for p in gh("project", "list", "--owner", ORG, "--format", "json", "--limit", "100")["projects"]
             if p["title"] == PROJECT_TITLE]
    if found:
        number = found[0]["number"]
    else:
        number = gh("project", "create", "--owner", ORG, "--title", PROJECT_TITLE, "--format", "json")["number"]
        changed("project created")
    p = project_state(number)
    pid = p["id"]
    have = {f["name"] for f in p["fields"]["nodes"] if f}
    for name, opts in PROJECT_FIELDS.items():
        if name in have:
            continue
        inp = {"projectId": pid, "name": name, "dataType": "DATE" if opts is None else "SINGLE_SELECT"}
        if opts:
            inp["singleSelectOptions"] = [{"name": o, "color": "GRAY", "description": ""} for o in opts]
        graphql("mutation AddField($i: CreateProjectV2FieldInput!) { createProjectV2Field(input: $i) { clientMutationId } }", i=inp)
        changed(f"project field {name} created")
    if REPO not in {r["nameWithOwner"] for r in p["repositories"]["nodes"]}:
        rid = gh("api", f"repos/{REPO}", "--jq", "{id: .node_id}")["id"]
        graphql("mutation Link($p: ID!, $r: ID!) { linkProjectV2ToRepository(input: {projectId: $p, repositoryId: $r}) { clientMutationId } }", p=pid, r=rid)
        changed("project linked to repo")
    if not p["public"] or p["shortDescription"] != PROJECT_BLURB:
        graphql("mutation Pub($p: ID!, $d: String!) { updateProjectV2(input: {projectId: $p, public: true, shortDescription: $d}) { clientMutationId } }", p=pid, d=PROJECT_BLURB)
        changed("project made public with its description")
    views = {v["name"]: v for v in p["views"]["nodes"]}
    # A new project's only view is "View 1"; it becomes the Backlog table.
    if "View 1" in views and "Backlog" not in views:
        views["Backlog"] = views.pop("View 1")
    for name, layout, filt in VIEWS:
        v = views.get(name)
        if v is None:
            mut = "mutation View($i: CreateProjectV2ViewInput!) { createProjectV2View(input: $i) { projectV2View { id name layout filter } } }"
            v = graphql(mut, i={"projectId": pid, "name": name, "layout": layout})["createProjectV2View"]["projectV2View"]
            changed(f"project view {name} created")
        if (v["name"], v["layout"], v["filter"] or "") != (name, layout, filt):
            mut = "mutation ViewEdit($i: UpdateProjectV2ViewInput!) { updateProjectV2View(input: $i) { clientMutationId } }"
            graphql(mut, i={"viewId": v["id"], "name": name, "layout": layout, "filter": filt})
            changed(f"project view {name} updated")
    p = project_state(number)
    fields = {f["name"]: (f["id"], {o["name"]: o["id"] for o in f.get("options", [])}) for f in p["fields"]["nodes"] if f}
    return {"id": pid, "number": number, "fields": fields}


# ------------------------------------------------------------ milestones

MILESTONES = {
    "v0.1 Launch": ("2026-11-15", "The list goes live: seeds imported, wiki and Discussions open, v0.1.0 tagged."),
    "v0.2 Trust & Trending": ("2026-12-15", "Make the Likes harder to game and the bots harder to knock over."),
    "v0.3 Polish": ("2027-01-31", "The small stuff that makes the site and the docs nicer to use."),
}


def milestones() -> dict:
    print("milestones")
    have = {m["title"]: m for m in paginate(f"repos/{REPO}/milestones?state=all&per_page=100")}
    out = {}
    for title, (due, desc) in MILESTONES.items():
        body = {"title": title, "description": desc, "due_on": f"{due}T12:00:00Z"}
        m = have.get(title)
        if m is None:
            m = api("POST", f"repos/{REPO}/milestones", body)
            changed(f"milestone {title} created")
        elif ((m["due_on"] or "")[:10], m["description"]) != (due, desc):
            m = api("PATCH", f"repos/{REPO}/milestones/{m['number']}", body)
            changed(f"milestone {title} updated")
        out[title] = m["number"]
    return out


# --------------------------------------------------------------- backlog

V1, V2, V3 = "v0.1 Launch", "v0.2 Trust & Trending", "v0.3 Polish"
EPIC_LAUNCH, EPIC_TRUST, EPIC_POLISH = "Launch v0.1", "Trust and anti-gaming", "Reliability and polish"
SEED, WIKI, DISCUSS = "Import the seed list", "Write the wiki", "Open Discussions with the welcome and how-Likes-work posts"


def issue(title, type_, area, priority, size, body, epic=None, milestone=None, blocked_by=(), target=None, labels=()):
    return {"title": title, "type": type_, "area": area, "priority": priority, "size": size, "body": body.strip() + "\n",
            "epic": epic, "milestone": milestone, "blocked_by": list(blocked_by), "target": target, "labels": list(labels)}


BACKLOG = [
    issue(EPIC_LAUNCH, "Epic", "docs", "high", "L", milestone=V1, body="""
Everything between "the bots work" and "people can use it". Right now the list has one entry, the wiki is empty and nobody's been told how Likes work. This epic is the short road to a launch we're happy to link to.

Done when every sub-issue is closed and v0.1.0 is tagged.
"""),
    issue(SEED, "Task", "data", "high", "M", epic=EPIC_LAUNCH, milestone=V1, body="""
The list has a single entry. Visit the site today and you'd reasonably think it's abandoned.

We have a seed list of about 470 repos. Once the seed subcommand lands, each one goes through the same checks a normal submission does, and anything that fails gets a human decision before it's added. The ones that pass land in one bot PR.

Why it takes a while: on-merge creates at most 40 Likes Discussions per run to stay under GitHub's content limits, and it runs hourly, so expect around 12 hours before every seed has its thread.

Done when the seeds that passed are in `data/entries.json`, every one of them has a Likes Discussion, and the site shows them.
"""),
    issue(WIKI, "Docs", "docs", "high", "M", epic=EPIC_LAUNCH, milestone=V1, labels=["documentation"], body="""
The README and the issue chooser both send you to the wiki, and right now the wiki is empty. That's a dead end on the first click.

Done when every page in the spec is live, along with a sidebar and footer, and every wiki link in the README and in `.github/ISSUE_TEMPLATE/config.yml` lands on a real page.
"""),
    issue(DISCUSS, "Docs", "docs", "high", "S", epic=EPIC_LAUNCH, milestone=V1, labels=["documentation"], body="""
Likes are the whole point of DevStream, and the only explanation of them is in the code. Someone landing in Discussions should see a welcome, then a plain account of how a Like is counted and why some don't count yet.

The categories already exist, including Likes. What's missing is the posts.

Done when the welcome is up in Announcements, "How Likes work" is up, there's a roadmap post in Ideas, and the first two are pinned.
"""),
    issue("Register for the OpenSSF Best Practices badge", "Security", "security", "medium", "S", epic=EPIC_LAUNCH, milestone=V1, body="""
Scorecard gives us 0 out of 10 on CII-Best-Practices for one reason: we haven't registered. Most of the answers are already true (signed releases, CodeQL, fuzzing, a security policy), so this is mostly filling in a form.

Registering needs a GitHub login at bestpractices.dev, so it's a browser job.

Done when the project has a bestpractices.dev entry at "passing" and the badge is in the README.
"""),
    issue("Tag v0.1.0 with signed provenance", "Task", "ci", "high", "S", epic=EPIC_LAUNCH, milestone=V1,
          blocked_by=[SEED, WIKI, DISCUSS], body="""
A version number tells people this is real, and provenance tells them the release came from this repo's workflow and nowhere else. Pushing a `v*` tag runs `release.yml`, which builds the release and attests it with `actions/attest-build-provenance`. Only admins can create `v*` tags.

It waits for the seeds, the wiki and the Discussions posts, because v0.1.0 is the version we announce.

Done when the v0.1.0 release exists, the CHANGELOG has its section, and `gh attestation verify` passes on the release asset.
"""),
    issue("Verify the org domain jamesconway.dev", "Security", "security", "medium", "S", epic=EPIC_LAUNCH, milestone=V1, body="""
Without a verified domain, anyone could claim to be the org behind devstream.jamesconway.dev and GitHub wouldn't say otherwise. Verifying puts a "Verified" badge on the org page and lets us restrict notification emails to the domain.

GitHub has no API for this. In the org settings under Verified and approved domains, add jamesconway.dev, copy the TXT record GitHub shows, add it in Cloudflare DNS, then press Verify.

Done when the org page shows jamesconway.dev as verified.
"""),
    issue("Upload the org avatar and the social preview image", "Task", "site", "low", "S", epic=EPIC_LAUNCH, milestone=V1, body="""
Share the repo link today and you get GitHub's generic card. The brand assets are already in `assets/`; they just need uploading, which only works through the browser.

Done when the org avatar is the DevStream mark and a shared repo link shows the social preview image.
"""),

    issue(EPIC_TRUST, "Epic", "security", "high", "L", milestone=V2, body="""
A ranked list is only worth reading if the ranking is honest. The anti-gaming rules shipped for launch cover the obvious attacks; this epic closes the gaps we found along the way and makes the bots fail safe when GitHub has a bad day.

Done when every sub-issue is closed.
"""),
    issue("Store each entry's repo ID to stop name-squat takeovers", "Security", "security", "high", "M", epic=EPIC_TRUST, milestone=V2, labels=["area: data"], body="""
Entries are keyed by `owner/name`. Say an owner deletes their account. Someone registers the same username, creates a repo with the same name, and the health check happily follows it. The listing, and every Like it earned, now points at a stranger's repo.

GitHub gives every repo a numeric ID that never changes and is never reused. If we save it when an entry is accepted and compare it on every health check, a squatter can't inherit anything.

Done when new entries store `repo_id`, existing entries are backfilled, and a mismatched ID sends the entry to `needs-review` instead of updating it.
"""),
    issue("Retry the Likes thread move after a failed stats write", "Bug", "bot", "medium", "M", epic=EPIC_TRUST, milestone=V2, body="""
When a repo is renamed, the health check moves its key in `discussions.json` on the stats branch. If that write fails after the rename PR has merged, nothing tries again. The run fails loudly, but the entry ends up with two Likes threads and its count split between them until someone fixes it by hand.

Done when a failed key move is picked up and finished on a later run (the sweeper is the natural place), with a test that fails the write once and checks the second run completes it.
"""),
    issue("Fall back to needs-review when a submission can't be decided", "Bug", "bot", "high", "M", epic=EPIC_TRUST, milestone=V2, body="""
If deciding a submission or posting the rejection fails, the run just fails. The sweeper re-dispatches it, it fails again, and the person who submitted hears nothing at all. Meanwhile every retry burns an Actions run.

Done when a submission that fails to decide or reject a set number of times gets the `needs-review` label and a short comment saying a human will take a look, and the sweeper stops retrying it.
"""),
    issue("Make Like-burst bookkeeping survive failed writes", "Bug", "bot", "medium", "M", epic=EPIC_TRUST, milestone=V2, body="""
Like-burst detection keeps its state in `flags.json`, and a few failure paths leave that state wrong:

- if the `flags.json` write itself fails, that run's new flags are lost;
- if updating the aggregate issue fails, its body goes stale;
- an issue closed while we're editing it can be missed;
- a single failed issue lookup skips the whole hour.

None of these lets anyone game the list, but each one hides a burst we should have looked at.

Done when each case has a test, a lost write is redone next run, and one bad lookup only skips that one entry.
"""),
    issue("Re-run hide jobs that were cancelled, and dedupe rot dispatches", "Bug", "bot", "medium", "S", epic=EPIC_TRUST, milestone=V2, body="""
When a report crosses the auto-hide threshold, a hide job is queued. If that job is cancelled before it runs, the report is left without a label and nothing goes back to it. Reports can also trigger a health check on the same entry more than once a day.

Done when the sweeper re-dispatches a report whose hide job never finished, the "no longer on the list" path has a test, and an entry gets at most one report-triggered health check per 24 hours.
"""),
    issue("Remember un-hides and let old reports expire", "Feature", "bot", "medium", "S", epic=EPIC_TRUST, milestone=V2, body="""
Two things make the report threshold blunter than it should be. If a maintainer un-hides an entry, the same open reports can hide it again; today the answer is to block whoever is abusing reports. And an open report counts forever, so three reports from two years ago weigh as much as three from this morning.

Done when an un-hide is recorded and only reports filed after it count, reports stop counting after a set age, and both have tests.
"""),
    issue("Catch prose restrictions inside an OSI licence text", "Bug", "bot", "low", "M", epic=EPIC_TRUST, milestone=V2, body="""
Licence detection rejects known non-OSI licences by name and marker. A LICENSE file that's the MIT text with "you may not use this commercially" tacked on still passes, because it looks like MIT.

Done when an added restriction after a recognised licence text fails the check with a clear reason, and the canonical SPDX texts in the test fixtures still pass.
"""),
    issue("Guard Discussion pagination against a repeated cursor", "Bug", "bot", "medium", "S", epic=EPIC_TRUST, milestone=V2, body="""
Listing the Likes Discussions follows `endCursor` page by page. If GitHub ever hands back the same cursor twice, we loop until the job times out. We also have no test that spans more than one page, so a pagination bug would only show up once there are 100+ entries. The REST helper has a cousin of this problem: a page that isn't a list.

Done when a repeated cursor stops the loop with an error, a non-list REST page raises, and there's a multi-page test for each.
"""),
    issue("Prune flags.json and the trust cache", "Task", "data", "low", "S", epic=EPIC_TRUST, milestone=V2, body="""
`flags.json` and the trust cache on the stats branch only ever grow. Flags for removed entries and reviewed flags past their quiet period stay forever, and so do cached trust results for accounts that haven't Liked anything in months.

Done when each run drops flags for entries no longer on the list and reviewed flags past their suppression window, and trust cache entries past their refresh age.
"""),
    issue("Move the Cloudflare secrets into a production environment", "Security", "ci", "medium", "S", epic=EPIC_TRUST, milestone=V2, body="""
The Cloudflare token is a repository secret, so any workflow in the repo can read it. Putting it in a `production` environment that only `main` can deploy to means a workflow on another branch never sees it. While we're in there, the deploy's `workflow_run` trigger should also check that the run was on `main`.

This needs the token re-entered, so it's easiest to do alongside the next rotation.

Done when `CLOUDFLARE_API_TOKEN` and the account ID live only in the `production` environment, that environment allows `main` alone, and the deploy job checks `head_branch == 'main'`.
"""),

    issue(EPIC_POLISH, "Epic", "bot", "medium", "L", milestone=V3, body="""
The small stuff. None of it is urgent on its own, but together it's the difference between code we trust and code we tiptoe around.

Done when every sub-issue is closed.
"""),
    issue("Harden the GitHub client's edge cases", "Bug", "bot", "medium", "M", epic=EPIC_POLISH, milestone=V3, body="""
The shared GitHub client handles the happy path and rate limits well. A handful of rarer answers still crash or misbehave:

- a redirect with no `Location`, or a relative one;
- `Retry-After` given as an HTTP date instead of seconds;
- network errors that escape as raw `URLError`s;
- a GraphQL error entry that isn't a dict, and GraphQL `RATE_LIMITED` errors that aren't retried.

The test fake has a matching gap: a redirect cycle loops forever.

Done when each of these has a test and raises a `GitHubError` or `RateLimited` the callers already handle.
"""),
    issue("Tidy the description cleaner's edge cases", "Bug", "bot", "low", "S", epic=EPIC_POLISH, milestone=V3, body="""
A few small ones in the description cleaner:

- a family emoji built with zero-width joiners counts as three characters;
- `?!?!` at the end becomes a full stop;
- `owner/repo.git.git` is accepted;
- a description containing `### Repository` is cut short without telling anyone.

Done when each has a test and does the sensible thing.
"""),
    issue("Sharpen the README and community checks", "Bug", "bot", "low", "S", epic=EPIC_POLISH, milestone=V3, body="""
The README quality check gets a few cases wrong. It says "add at least two" when you already have one. It treats `#hashtag` lines as headings and drops them, and it doesn't strip setext headings. A missing community profile (404) is silently read as "nothing there", and a README whose base64 won't decode can pass as a short README.

Done when each case has a test, the message reflects what's actually missing, and undecodable content is treated as undecided instead of short.
"""),
    issue("Make entries.json saves atomic and tighten validation", "Task", "data", "low", "S", epic=EPIC_POLISH, milestone=V3, body="""
`save()` writes `entries.json` in place, so a crash halfway through leaves a broken file. Validation also lets `"version": true` through because `True == 1` in Python, and the list of statuses is written out twice.

Done when saves go through a temp file and a rename, the version check rejects booleans, statuses come from one place, and the validator's tests check the messages and the CLI's exit code.
"""),
    issue("Stop a failing cleanup from keeping a submission open", "Bug", "bot", "low", "S", epic=EPIC_POLISH, milestone=V3, body="""
Two loose ends after a submission merges. If the follow-up comment or label fails, an entry that's already on the list gets marked `needs-review`. And if deleting the branch keeps failing, the submission issue never closes.

Done when a post-merge comment failure retries instead of flagging the entry, and a branch that won't delete is logged and the issue closes anyway.
"""),
    issue("Switch the App token step from app-id to client-id", "Task", "ci", "low", "S", epic=EPIC_POLISH, milestone=V3, body="""
`actions/create-github-app-token` has deprecated the `app-id` input in favour of `client-id`. It still works, but one day it won't, and every bot workflow uses it.

Done when there's a `CLIENT_ID` secret, every workflow passes `client-id`, and the runs are green.
"""),
    issue("Check the three Wrangler pins agree", "Task", "ci", "low", "S", epic=EPIC_POLISH, milestone=V3, body="""
Wrangler is pinned in `site/package.json`, in its lockfile, and as `WRANGLER_VERSION` in `deploy.yml`. Dependabot ignores it on purpose because it can't update all three together, so a hand bump that misses one would deploy a different version from the one we tested.

Done when a test fails if the three versions differ.
"""),
    issue("Smooth the rough edges in setup_repo.sh", "Task", "ci", "low", "S", epic=EPIC_POLISH, milestone=V3, body="""
`scripts/admin/setup_repo.sh` exits 1 at the last step until the org lets Actions approve PRs, which makes a good run look like a failure. Two rulesets with the same name would also break its ID lookup. And there's an empty `.prettierrc.json` that does nothing.

Done when the script reports the org setting clearly instead of failing, the ruleset lookup copes with duplicates, and the empty file is either filled or gone.
"""),

    issue("Rotate the Cloudflare API token", "Task", "ci", "high", "S", target="2027-10-05", body="""
The Cloudflare API token the deploy uses expires around 2027-11-05. When it does, every deploy fails and the site stops updating. This issue's target date leaves a month of slack.

To rotate it:

1. In the Cloudflare dashboard, create a new API token with the same permissions as the current one (Cloudflare Pages edit and DNS edit on the jamesconway.dev zone) and an expiry about a year out.
2. Run `gh secret set CLOUDFLARE_API_TOKEN -R devstreamhq/devstream` and paste the token when it asks. If the secrets have moved to the `production` environment by then, add `--env production`.
3. Re-run the latest Deploy workflow and check it's green.
4. Revoke the old token in Cloudflare.
5. Open the next rotation issue for a year from now.

Done when a deploy succeeds on the new token and the old one is revoked.
"""),
]


def backlog(miles: dict, proj: dict, fields: dict, rewrite: bool):
    print("backlog")
    have = {i["title"]: i for i in paginate(f"repos/{REPO}/issues?state=all&creator={OWNER_LOGIN}&per_page=100")
            if "pull_request" not in i}
    done = {}
    for spec in BACKLOG:
        labels = [f"priority: {spec['priority']}", f"area: {spec['area']}"] + spec["labels"]
        ms = miles.get(spec["milestone"])
        i = have.get(spec["title"])
        if i is None:
            body = {"title": spec["title"], "body": spec["body"], "labels": labels, "type": spec["type"]}
            if ms:
                body["milestone"] = ms
            i = api("POST", f"repos/{REPO}/issues", body)
            changed(f"issue #{i['number']} {spec['title']} created")
        else:
            n = i["number"]
            missing = [x for x in labels if x not in {l["name"] for l in i["labels"]}]
            if missing:
                api("POST", f"repos/{REPO}/issues/{n}/labels", {"labels": missing})
                changed(f"issue #{n} labels added: {missing}")
            patch = {}
            if ms and not i.get("milestone"):
                patch["milestone"] = ms
            if not i.get("type"):
                patch["type"] = spec["type"]
            if rewrite and (i["body"] or "") != spec["body"]:
                patch["body"] = spec["body"]
            if patch:
                api("PATCH", f"repos/{REPO}/issues/{n}", patch)
                changed(f"issue #{n} updated: {sorted(patch)}")
        done[spec["title"]] = i

    for spec in BACKLOG:
        n = done[spec["title"]]["number"]
        if spec["epic"]:
            epic = done[spec["epic"]]["number"]
            subs = {s["id"] for s in paginate(f"repos/{REPO}/issues/{epic}/sub_issues?per_page=100")}
            if done[spec["title"]]["id"] not in subs:
                api("POST", f"repos/{REPO}/issues/{epic}/sub_issues", {"sub_issue_id": done[spec["title"]]["id"]})
                changed(f"#{n} added under epic #{epic}")
        if spec["blocked_by"]:
            blockers = {b["id"] for b in paginate(f"repos/{REPO}/issues/{n}/dependencies/blocked_by?per_page=100")}
            for t in spec["blocked_by"]:
                if done[t]["id"] not in blockers:
                    api("POST", f"repos/{REPO}/issues/{n}/dependencies/blocked_by", {"issue_id": done[t]["id"]})
                    changed(f"#{n} blocked by #{done[t]['number']}")
        place(done[spec["title"]], spec, proj, fields)


ITEM_QUERY = """query Item($id: ID!) { node(id: $id) { ... on Issue {
  issueFieldValues(first: 30) { nodes {
    ... on IssueFieldSingleSelectValue { field { ... on IssueFieldSingleSelect { id } } }
    ... on IssueFieldDateValue { field { ... on IssueFieldDate { id } } } } }
  projectItems(first: 20) { nodes { id project { id } fieldValues(first: 30) { nodes {
    ... on ProjectV2ItemFieldSingleSelectValue { field { ... on ProjectV2SingleSelectField { id } } }
    ... on ProjectV2ItemFieldDateValue { field { ... on ProjectV2Field { id } } } } } } } } } }"""

AREA = {"bot": "Bot", "site": "Site", "data": "Data", "docs": "Docs", "ci": "CI", "security": "Security"}


def place(i: dict, spec: dict, proj: dict, fields: dict):
    """Fill empty issue fields and project fields; never overwrite a value someone set."""
    n, node = i["number"], i["node_id"]
    state = graphql(ITEM_QUERY, id=node)["node"]
    want = {"Priority": spec["priority"].title(), "Area": AREA[spec["area"]], "Effort": spec["size"]}
    if fields:
        set_ids = {v["field"]["id"] for v in state["issueFieldValues"]["nodes"] if v and v.get("field")}
        values = [{"fieldId": fields[k][0], "singleSelectOptionId": fields[k][1][v]}
                  for k, v in want.items() if k in fields and fields[k][0] not in set_ids]
        if spec["target"] and "Target date" in fields and fields["Target date"][0] not in set_ids:
            values.append({"fieldId": fields["Target date"][0], "dateValue": spec["target"]})
        if values:
            graphql("mutation SetFields($i: ID!, $f: [IssueFieldCreateOrUpdateInput!]!) { setIssueFieldValue(input: {issueId: $i, issueFields: $f}) { clientMutationId } }", i=node, f=values)
            changed(f"#{n} issue fields set")

    item = next((x for x in state["projectItems"]["nodes"] if x["project"]["id"] == proj["id"]), None)
    if item is None:
        mut = "mutation AddItem($p: ID!, $c: ID!) { addProjectV2ItemById(input: {projectId: $p, contentId: $c}) { item { id } } }"
        try:
            item = {"id": graphql(mut, p=proj["id"], c=node)["addProjectV2ItemById"]["item"]["id"], "fieldValues": {"nodes": []}}
            changed(f"#{n} added to the project")
        except GhError as e:
            # The project's auto-add workflow adds sub-issues on its own, sometimes between our read and our add.
            if "already exists" not in str(e):
                raise
            nodes = graphql(ITEM_QUERY, id=node)["node"]["projectItems"]["nodes"]
            item = next(x for x in nodes if x["project"]["id"] == proj["id"])
    filled = {v["field"]["id"] for v in item["fieldValues"]["nodes"] if v and v.get("field")}
    pf = proj["fields"]
    sets = {"Status": "Todo", "Priority": want["Priority"], "Size": spec["size"], "Area": want["Area"]}
    for name, opt in sets.items():
        fid, opts = pf[name]
        if fid not in filled:
            update_item(proj["id"], item["id"], fid, {"singleSelectOptionId": opts[opt]})
            changed(f"#{n} project {name} = {opt}")
    if spec["target"] and pf["Target date"][0] not in filled:
        update_item(proj["id"], item["id"], pf["Target date"][0], {"date": spec["target"]})
        changed(f"#{n} project Target date = {spec['target']}")


def update_item(pid, item, field, value):
    mut = "mutation SetItem($p: ID!, $i: ID!, $f: ID!, $v: ProjectV2FieldValue!) { updateProjectV2ItemFieldValue(input: {projectId: $p, itemId: $i, fieldId: $f, value: $v}) { clientMutationId } }"
    graphql(mut, p=pid, i=item, f=field, v=value)


# --------------------------------------------------------- repo and org

REPO_DESCRIPTION = "Repos ranked by the developers who like them. Submit yours, Like what you use."
ORG_SETTINGS = {
    "name": "DevStream",
    "description": "We run DevStream: open-source repos, ranked by the developers who Like them.",
    "blog": "https://devstream.jamesconway.dev",
    "default_repository_permission": "read",
    "members_can_create_public_repositories": False,
    "members_can_create_private_repositories": False,
}
SECURITY_CONFIG = {
    "name": "DevStream defaults",
    "description": "Dependency alerts and updates, secret scanning and push protection for every new repo.",
    # Free on public repos; GitHub refuses secret scanning in a configuration without it.
    "advanced_security": "enabled",
    "dependency_graph": "enabled",
    "dependabot_alerts": "enabled",
    "dependabot_security_updates": "enabled",
    "secret_scanning": "enabled",
    "secret_scanning_push_protection": "enabled",
    "private_vulnerability_reporting": "enabled",
    # Our repos run CodeQL from their own workflow; default setup would clash with it.
    "code_scanning_default_setup": "disabled",
}
ORG_RULESET = {
    "name": "default-branch",
    "target": "branch",
    "enforcement": "active",
    "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []},
                   "repository_name": {"include": ["~ALL"], "exclude": []}},
    "bypass_actors": [{"actor_id": 1, "actor_type": "OrganizationAdmin", "bypass_mode": "always"}],
    "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}],
}


def repo_meta():
    print("repo")
    r = api("GET", f"repos/{REPO}")
    if r["description"] != REPO_DESCRIPTION:
        api("PATCH", f"repos/{REPO}", {"description": REPO_DESCRIPTION})
        changed("repo description")


def org():
    print("org")
    o = api("GET", f"orgs/{ORG}")
    diff = {k: v for k, v in ORG_SETTINGS.items() if o.get(k) != v}
    if diff:
        api("PATCH", f"orgs/{ORG}", diff)
        changed(f"org settings: {sorted(diff)}")


def security_defaults():
    configs = {c["name"]: c for c in api("GET", f"orgs/{ORG}/code-security/configurations")}
    c = configs.get(SECURITY_CONFIG["name"])
    if c is None:
        c = api("POST", f"orgs/{ORG}/code-security/configurations", SECURITY_CONFIG)
        changed("security configuration created")
    elif any(c.get(k) != v for k, v in SECURITY_CONFIG.items()):
        c = api("PATCH", f"orgs/{ORG}/code-security/configurations/{c['id']}", SECURITY_CONFIG)
        changed("security configuration updated")
    defaults = api("GET", f"orgs/{ORG}/code-security/configurations/defaults") or []
    if not any(d["configuration"]["id"] == c["id"] and d["default_for_new_repos"] == "all" for d in defaults):
        api("PUT", f"orgs/{ORG}/code-security/configurations/{c['id']}/defaults", {"default_for_new_repos": "all"})
        changed("security configuration is the default for new repos")


def org_ruleset():
    have = {r["name"]: r for r in api("GET", f"orgs/{ORG}/rulesets")}
    r = have.get(ORG_RULESET["name"])
    if r is None:
        api("POST", f"orgs/{ORG}/rulesets", ORG_RULESET)
        changed("org ruleset created")


def main():
    rewrite = "--rewrite" in sys.argv[1:]
    labels()
    optional("issue types", issue_types)
    fields = {}

    def _fields():
        nonlocal fields
        issue_fields()
        fields = issue_field_ids()
    optional("issue fields", _fields)
    proj = project()
    miles = milestones()
    backlog(miles, proj, fields, rewrite)
    repo_meta()
    org()
    optional("security defaults for new repos", security_defaults)
    optional("org ruleset", org_ruleset)
    notes.append("verified domain: no API; start it in org settings")
    return summary()


def summary() -> int:
    """Print the run's outcome; 2 if any optional step was skipped, so a partial run can't pass for a clean one."""
    print(f"\n{len(changes)} change(s)")
    for n in notes:
        print(f"note: {n}")
    if not skipped:
        return 0
    print(f"\n!!! NOT DONE: {len(skipped)} step(s) skipped !!!")
    for s in skipped:
        print(f"!!! NOT DONE: {s}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
