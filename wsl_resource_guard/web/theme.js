"use strict";

// Loaded before the stylesheet so the saved theme is applied on the first paint.
(() => {
  const key = "wrg-theme";
  const choices = ["system", "light", "dark", "warm"];
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  const valid = (value) => choices.includes(value) ? value : "system";
  let preference = "system";
  try {
    preference = valid(localStorage.getItem(key));
  } catch {}

  // Matches --bg in style.css for each theme.
  const themeColors = { light: "#f5f7fa", dark: "#101923", warm: "#f4efe5" };

  function apply() {
    const theme = preference === "system"
      ? (system.matches ? "dark" : "light") : preference;
    document.documentElement.dataset.theme = theme;
    const picker = document.getElementById("theme-select");
    if (picker) picker.value = preference;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = themeColors[theme] || themeColors.light;
  }

  apply();
  system.addEventListener("change", () => {
    if (preference === "system") apply();
  });
  window.addEventListener("storage", (event) => {
    if (event.key === key || event.key === null) {
      preference = valid(event.newValue);
      apply();
    }
  });
  document.addEventListener("DOMContentLoaded", () => {
    const picker = document.getElementById("theme-select");
    if (!picker) return;
    picker.value = preference;
    picker.addEventListener("change", () => {
      preference = valid(picker.value);
      try {
        localStorage.setItem(key, preference);
      } catch {}
      apply();
    });
  });
})();
