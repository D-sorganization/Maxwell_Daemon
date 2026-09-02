// Pre-paint theme bootstrap. Reads localStorage["maxwell-theme"] and
// falls back to prefers-color-scheme. Runs before <body> paints to
// avoid a flash of the wrong theme.
(function () {
  try {
    var stored = localStorage.getItem("maxwell-theme");
    var prefersDark = window.matchMedia
      && window.matchMedia("(prefers-color-scheme: dark)").matches;
    var theme = (stored === "dark" || stored === "light")
      ? stored
      : (prefersDark ? "dark" : "light");
    document.documentElement.setAttribute("data-theme", theme);
  } catch (e) {
    var prefersDarkFallback = false;
    try {
      prefersDarkFallback = !!(window.matchMedia
        && window.matchMedia("(prefers-color-scheme: dark)").matches);
    } catch (e2) { /* matchMedia also unavailable; default to light */ }
    document.documentElement.setAttribute(
      "data-theme",
      prefersDarkFallback ? "dark" : "light"
    );
  }
})();
