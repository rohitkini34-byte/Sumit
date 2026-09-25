// Light/dark toggle. Sets data-theme on <html> and remembers the choice.

const systemDark = window.matchMedia("(prefers-color-scheme: dark)");

function effectiveTheme() {
  const forced = document.documentElement.getAttribute("data-theme");
  if (forced === "light" || forced === "dark") return forced;
  return systemDark.matches ? "dark" : "light";
}

export function initTheme() {
  const root = document.documentElement;
  document.querySelectorAll(".theme-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const next = effectiveTheme() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("theme", next); } catch (e) { /* storage unavailable */ }
    });
  });
}
