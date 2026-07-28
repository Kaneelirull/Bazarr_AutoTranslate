(() => {
  const form = document.getElementById("log-filters");
  const output = document.getElementById("log-output");
  const status = document.getElementById("log-status");
  const more = document.getElementById("load-more");
  const refresh = document.getElementById("refresh-button");
  const theme = document.getElementById("theme-toggle");
  let cursor = null;

  const query = (nextCursor = null) => {
    const params = new URLSearchParams(new FormData(form));
    params.set("limit", "200");
    if (nextCursor !== null) params.set("cursor", String(nextCursor));
    return params;
  };

  const load = async (append = false) => {
    status.textContent = "Loading logs...";
    more.disabled = true;
    refresh.disabled = true;
    refresh.textContent = "Refreshing...";
    output.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(`/api/logs?${query(append ? cursor : null)}`, {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`Log request failed (${response.status})`);
      const payload = await response.json();
      const text = (payload.lines || []).join("\n");
      output.textContent = append && output.textContent
        ? `${output.textContent}\n${text}`
        : text;
      cursor = payload.nextCursor;
      more.hidden = cursor === null;
      status.textContent = payload.lines?.length
        ? `Showing ${output.textContent.split("\n").length} sanitized records`
        : "No matching log records.";
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : "Logs unavailable";
    } finally {
      more.disabled = false;
      refresh.disabled = false;
      refresh.textContent = "Refresh now";
      output.setAttribute("aria-busy", "false");
    }
  };

  const currentTheme = () => document.documentElement.dataset.theme || "dark";

  const updateThemeButton = () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    theme.textContent = `${next === "light" ? "Light" : "Dark"} theme`;
    theme.setAttribute("aria-label", `Switch to ${next} theme`);
  };

  const setTheme = (value) => {
    document.documentElement.dataset.theme = value;
    try {
      localStorage.setItem("dashboard-theme", value);
    } catch (_error) {
      // Theme persistence is optional.
    }
    updateThemeButton();
  };

  const applyInitialTheme = () => {
    let saved = null;
    try {
      saved = localStorage.getItem("dashboard-theme");
    } catch (_error) {
      saved = null;
    }
    const system = window.matchMedia("(prefers-color-scheme: light)").matches
      ? "light"
      : "dark";
    setTheme(saved === "light" || saved === "dark" ? saved : system);
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    cursor = null;
    load(false);
  });
  more.addEventListener("click", () => load(true));
  refresh.addEventListener("click", () => {
    cursor = null;
    load(false);
  });
  theme.addEventListener("click", () => {
    setTheme(currentTheme() === "dark" ? "light" : "dark");
  });
  applyInitialTheme();
  load(false);
})();
