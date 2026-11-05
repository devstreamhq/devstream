const $ = (id) => document.getElementById(id);
const q = $("q");
const rows = $("rows");

// Elsewhere the search box sends you to the home page with your query.
const term = new URLSearchParams(location.search).get("q");
if (q && term) q.value = term;
q?.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !rows)
    location.href = "/?q=" + encodeURIComponent(q.value.trim());
});
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !/^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) {
    e.preventDefault();
    q?.focus();
  }
});

const side = document.querySelector("[data-side]");
if (side) {
  const narrow = matchMedia("(max-width: 900px)");
  const fold = () => (side.open = !narrow.matches);
  fold();
  narrow.addEventListener("change", fold);
}

if (rows) {
  const all = [...rows.rows];
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const heads = [...document.querySelectorAll("th[data-sort]")];
  const lang = $("lang");
  const more = $("more");
  const panel = $("panel");
  const ORDER = { top: ["i", 1], trending: ["delta", -1], new: ["new", -1] };
  const COL = { rank: "i", likes: "likes", stars: "stars", pushed: "pushed" };
  let tab = "top";
  let [key, dir] = ORDER.top;
  let limit = 20;
  let list = all;
  const n = (r, k) => +r.dataset[k];

  const render = () => {
    const t = q.value.trim().toLowerCase();
    list = all.filter(
      (r) =>
        (tab !== "trending" || r.dataset.trend) &&
        (!lang.value || r.dataset.lang === lang.value) &&
        (!t || r.dataset.q.includes(t)),
    );
    list.sort((a, b) => dir * (n(a, key) - n(b, key)) || n(a, "i") - n(b, "i"));
    rows.append(...list, ...all.filter((r) => !list.includes(r)));
    all.forEach((r) => (r.hidden = !list.slice(0, limit).includes(r)));
    more.hidden = list.length <= limit;
    $("none").hidden = list.length > 0;
    $("count").textContent =
      `${Math.min(limit, list.length)} of ${list.length} repos`;
    for (const th of heads) {
      const on = COL[th.dataset.sort] === key;
      th.setAttribute(
        "aria-sort",
        on ? (dir > 0 ? "ascending" : "descending") : "none",
      );
    }
  };

  const select = (btn) => {
    for (const b of tabs) {
      b.setAttribute("aria-selected", b === btn);
      b.tabIndex = b === btn ? 0 : -1;
    }
    panel.setAttribute("aria-labelledby", btn.id);
    tab = btn.dataset.tab;
    [key, dir] = ORDER[tab];
    limit = 20;
    render();
  };
  tabs.forEach((b, i) => {
    b.addEventListener("click", () => select(b));
    b.addEventListener("keydown", (e) => {
      const to = {
        ArrowRight: i + 1,
        ArrowLeft: i - 1,
        Home: 0,
        End: tabs.length - 1,
      }[e.key];
      if (to === undefined) return;
      e.preventDefault();
      const next = tabs[(to + tabs.length) % tabs.length];
      next.focus();
      select(next);
    });
  });

  for (const th of heads) {
    th.querySelector("button").addEventListener("click", () => {
      const k = COL[th.dataset.sort];
      dir = k === key ? -dir : k === "i" ? 1 : -1;
      key = k;
      render();
    });
  }

  const reset = () => {
    limit = 20;
    render();
  };
  q.addEventListener("input", reset);
  lang.addEventListener("change", reset);
  more.addEventListener("click", () => {
    const first = limit;
    limit += 20;
    render();
    list[first]?.querySelector("a")?.focus();
  });
  render();
}
