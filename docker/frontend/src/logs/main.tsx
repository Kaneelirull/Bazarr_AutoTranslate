import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { LogsApp } from "./App";

const root = document.getElementById("logs-root");
if (!root) throw new Error("Logs root is unavailable");
createRoot(root).render(<StrictMode><LogsApp /></StrictMode>);
