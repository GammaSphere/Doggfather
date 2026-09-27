// Pairwise mode: left/right arrows (or A/B) pick a side.
(() => {
  "use strict";
  const pick = (side) => {
    const form = document.querySelector(`form[data-side="${side}"]`);
    if (form) form.requestSubmit();
  };
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select") || e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.key === "ArrowLeft" || e.key === "a" || e.key === "A") { pick("A"); e.preventDefault(); }
    if (e.key === "ArrowRight" || e.key === "b" || e.key === "B") { pick("B"); e.preventDefault(); }
  });
})();
