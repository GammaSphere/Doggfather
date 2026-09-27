// Live organizer dashboard: poll the progress JSON and patch the page.
// Values are written with textContent, never innerHTML.
(() => {
  "use strict";
  const root = document.querySelector("[data-progress]");
  if (!root) return;
  const url = root.dataset.progress;
  const tbody = root.querySelector("[data-judges]");

  const cell = (text, className) => {
    const td = document.createElement("td");
    if (className) td.className = className;
    td.textContent = text;
    return td;
  };

  const bar = (done, total) => {
    const td = document.createElement("td");
    const outer = document.createElement("div");
    outer.className = "progress progress--teal";
    const inner = document.createElement("span");
    inner.style.setProperty("--pct", (total ? Math.round(100 * done / total) : 0) + "%");
    outer.appendChild(inner);
    td.appendChild(outer);
    return td;
  };

  const ago = (iso) => {
    if (!iso) return "never";
    const minutes = Math.round((Date.now() - new Date(iso)) / 60000);
    if (minutes < 60) return Math.max(minutes, 1) + "m ago";
    if (minutes < 1440) return Math.floor(minutes / 60) + "h ago";
    return Math.floor(minutes / 1440) + "d ago";
  };

  const render = (snap) => {
    const totals = snap.totals;
    const set = (field, value) => root.querySelectorAll(`[data-field="${field}"]`).forEach((el) => { el.textContent = value; });
    set("pct", totals.pct + "%");
    set("done", totals.done);
    set("assigned", totals.assigned);
    set("pending", totals.pending);
    set("delinquent", totals.delinquent);
    set("generated_at", new Date(snap.generated_at).toLocaleTimeString());
    if (!tbody) return;
    tbody.replaceChildren(...snap.judges.map((j) => {
      const tr = document.createElement("tr");
      const name = cell(j.name);
      name.style.fontWeight = "700";
      const state = cell(`[ ${j.state.toUpperCase()} ]`, "status " + (j.delinquent ? "status--pink" : j.state === "finished" ? "status--ok" : "status--open"));
      tr.append(name, state, cell(`${j.done} / ${j.assigned}`, "num"), bar(j.done, j.assigned), cell(ago(j.last_activity), "small"));
      return tr;
    }));
  };

  const poll = async () => {
    try {
      const response = await fetch(url, { headers: { accept: "application/json" }, credentials: "same-origin" });
      if (response.ok) render(await response.json());
    } catch { /* offline or restarting: try again next tick */ }
  };
  setInterval(poll, 15000);
})();
