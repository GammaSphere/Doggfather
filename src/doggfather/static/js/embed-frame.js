// Runs inside the embedded gallery: tell the host page our height.
(() => {
  "use strict";
  if (window.parent === window) return;
  let last = 0;
  const report = () => {
    const height = document.documentElement.scrollHeight;
    if (Math.abs(height - last) > 2) {
      last = height;
      window.parent.postMessage({ doggfather: "resize", height }, "*");
    }
  };
  window.addEventListener("load", report);
  window.addEventListener("resize", report);
  if ("ResizeObserver" in window) new ResizeObserver(report).observe(document.body);
  report();
})();
