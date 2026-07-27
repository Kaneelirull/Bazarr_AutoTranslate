(() => {
  const form = document.getElementById("log-filters");
  const output = document.getElementById("log-output");
  const status = document.getElementById("log-status");
  const more = document.getElementById("load-more");
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
    }
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    cursor = null;
    load(false);
  });
  more.addEventListener("click", () => load(true));
  load(false);
})();
