import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { DashboardApp } from "./App";
import type { StatusSnapshot } from "./types";

const root = document.getElementById("dashboard-root");
if (!root) throw new Error("Dashboard root is unavailable");
let initialSnapshot: StatusSnapshot = {};
try { initialSnapshot = JSON.parse(root.dataset.snapshot || "{}"); }
catch { initialSnapshot = { service: { phase: "startup" } }; }
root.removeAttribute("data-snapshot");
createRoot(root).render(<StrictMode><DashboardApp initialSnapshot={initialSnapshot} timeZone={root.dataset.timeZone || "UTC"} /></StrictMode>);
