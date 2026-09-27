// Doggfather: progressive enhancement only. Every page works without JS.
(() => {
  "use strict";

  // Mobile navigation drawer.
  const links = document.getElementById("nav-links");
  const scrim = document.querySelector("[data-nav-scrim]");
  const burger = document.querySelector(".nav__burger");
  const setNav = (open) => {
    if (!links) return;
    links.classList.toggle("is-open", open);
    scrim && scrim.classList.toggle("is-open", open);
    burger && burger.setAttribute("aria-expanded", String(open));
    document.documentElement.classList.toggle("nav-open", open);
  };
  document.querySelectorAll("[data-nav-toggle]").forEach((el) =>
    el.addEventListener("click", () => setNav(!links.classList.contains("is-open")))
  );
  scrim && scrim.addEventListener("click", () => setNav(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setNav(false); });

  // Copy-to-clipboard buttons: <button data-copy="#input-id">.
  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const target = document.querySelector(btn.dataset.copy);
      if (!target) return;
      const text = target.value || target.textContent;
      try {
        await navigator.clipboard.writeText(text.trim());
      } catch {
        target.select && target.select();
        document.execCommand("copy");
      }
      const label = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(() => { btn.textContent = label; }, 1400);
    });
  });

  // Confirm destructive actions: <form data-confirm="Really?">.
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (e) => {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  // Auto-submit filter controls: <select data-autosubmit>.
  document.querySelectorAll("[data-autosubmit]").forEach((el) => {
    el.addEventListener("change", () => el.form && el.form.requestSubmit());
  });

  // Local time hints for UTC timestamps.
  document.querySelectorAll("time[datetime]").forEach((el) => {
    const d = new Date(el.getAttribute("datetime"));
    if (!isNaN(d)) el.title = d.toLocaleString() + " (your time)";
  });

  // Live countdowns: <span data-countdown="2026-03-01T18:00:00Z">.
  const countdowns = document.querySelectorAll("[data-countdown]");
  if (countdowns.length) {
    const tick = () => countdowns.forEach((el) => {
      const ms = new Date(el.dataset.countdown) - new Date();
      if (ms <= 0) { el.textContent = "00:00:00"; return; }
      const s = Math.floor(ms / 1000);
      const d = Math.floor(s / 86400);
      const hms = [Math.floor(s % 86400 / 3600), Math.floor(s % 3600 / 60), s % 60]
        .map((n) => String(n).padStart(2, "0")).join(":");
      el.textContent = (d ? d + "d " : "") + hms;
    });
    tick();
    setInterval(tick, 1000);
  }
})();
