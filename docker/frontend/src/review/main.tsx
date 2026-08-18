import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ReviewApp } from "./App";

const root = document.getElementById("review-root");
if (!root) throw new Error("Manual review root is unavailable");
createRoot(root).render(<StrictMode><ReviewApp timeZone={root.dataset.timeZone || "UTC"} /></StrictMode>);
