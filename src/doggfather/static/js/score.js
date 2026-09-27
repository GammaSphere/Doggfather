// Keyboard-first scoring. Digits score the active criterion and advance,
// arrows move, C jumps to the comment, Enter saves and opens the next
// project. The form still submits normally without JS.
(() => {
  "use strict";
  const form = document.querySelector("[data-scoring]");
  if (!form || form.querySelector("fieldset[disabled]")) return;
  const rows = Array.from(form.querySelectorAll(".criterion"));
  const preview = form.querySelector("[data-preview]");
  const comment = form.querySelector("textarea");
  if (!rows.length) return;

  let active = rows.findIndex((r) => !r.querySelector("input:checked"));
  if (active < 0) active = 0;

  const setActive = (i) => {
    active = Math.max(0, Math.min(rows.length - 1, i));
    rows.forEach((r, k) => r.classList.toggle("is-active", k === active));
  };

  const update = () => {
    let sum = 0;
    let weight = 0;
    rows.forEach((r) => {
      const checked = r.querySelector("input:checked");
      const w = parseFloat(r.dataset.weight || "0");
      if (checked && w > 0) {
        sum += w * parseInt(checked.value, 10);
        weight += w;
      }
    });
    if (preview) preview.textContent = weight ? (sum / weight).toFixed(2) : "—";
  };

  form.addEventListener("change", update);
  rows.forEach((r, i) => r.addEventListener("click", () => setActive(i)));

  document.addEventListener("keydown", (e) => {
    if (e.target.matches("textarea, input[type=text], input[type=search]")) {
      if (e.key === "Escape") e.target.blur();
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) form.requestSubmit();
      return;
    }
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    if (/^[0-9]$/.test(e.key)) {
      const value = e.key === "0" ? "10" : e.key;
      const input = rows[active].querySelector(`input[value="${value}"]`);
      if (input) {
        input.checked = true;
        update();
        setActive(active + 1);
        e.preventDefault();
      }
      return;
    }
    if (e.key === "ArrowDown" || e.key === "j") { setActive(active + 1); e.preventDefault(); }
    else if (e.key === "ArrowUp" || e.key === "k") { setActive(active - 1); e.preventDefault(); }
    else if (e.key === "c" || e.key === "C") { comment && comment.focus(); e.preventDefault(); }
    else if (e.key === "Enter") { form.requestSubmit(); e.preventDefault(); }
  });

  setActive(active);
  update();
})();
