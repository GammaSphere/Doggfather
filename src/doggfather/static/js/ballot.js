// Quadratic ballot helper: live credit meter, +/- steppers, and a hard stop
// at the budget. The server enforces the same budget; this only helps.
(() => {
  "use strict";
  const form = document.querySelector("[data-ballot]");
  if (!form) return;
  const credits = parseInt(form.dataset.credits, 10);
  const remaining = form.querySelector("[data-remaining]");
  const inputs = Array.from(form.querySelectorAll('input[type="number"]'));

  const value = (input) => Math.max(0, parseInt(input.value || "0", 10) || 0);
  const spent = () => inputs.reduce((sum, input) => sum + value(input) ** 2, 0);

  const refresh = () => {
    const used = spent();
    remaining.textContent = credits - used;
    remaining.classList.toggle("pink", used <= credits);
    remaining.classList.toggle("amber", used > credits);
    inputs.forEach((input) => {
      const row = input.closest(".ballot-row");
      const cost = row && row.querySelector("[data-cost]");
      if (cost) cost.textContent = `${value(input) ** 2} credits`;
      const plus = row && row.querySelector('[data-step="1"]');
      if (plus) plus.disabled = used - value(input) ** 2 + (value(input) + 1) ** 2 > credits;
    });
  };

  form.addEventListener("click", (e) => {
    const button = e.target.closest("[data-step]");
    if (!button) return;
    const input = button.parentElement.querySelector("input");
    input.value = Math.max(0, value(input) + parseInt(button.dataset.step, 10));
    refresh();
  });
  form.addEventListener("input", refresh);
  form.addEventListener("submit", (e) => {
    if (spent() > credits) {
      e.preventDefault();
      window.alert(`That ballot costs ${spent()} credits; you have ${credits}.`);
    }
  });
  refresh();
})();
