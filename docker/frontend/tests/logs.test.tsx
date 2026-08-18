import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LogsApp } from "../src/logs/App";

afterEach(() => vi.unstubAllGlobals());

describe("LogsApp", () => {
  it("filters, renders log text safely, and appends older records", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ lines: [], nextCursor: null }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ lines: ["<script>unsafe</script>"], nextCursor: 200 }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ lines: ["older"], nextCursor: null }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<LogsApp />);

    await user.type(screen.getByLabelText("Search text"), "timeout");
    await user.click(screen.getByRole("button", { name: "Filter" }));
    expect(await screen.findByText("<script>unsafe</script>")).toBeInTheDocument();
    expect(document.querySelector("script")).not.toBeInTheDocument();
    expect(String(fetchMock.mock.calls[1][0])).toContain("q=timeout");

    await user.click(screen.getByRole("button", { name: "Load older" }));
    await waitFor(() => expect(screen.getByText(/older/)).toBeInTheDocument());
    expect(screen.getByRole("status")).toHaveTextContent("2 sanitized records");
  });

  it("surfaces request failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: "logs unavailable" }), { status: 503 })));
    const user = userEvent.setup();
    render(<LogsApp />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("logs unavailable"));
  });
});
