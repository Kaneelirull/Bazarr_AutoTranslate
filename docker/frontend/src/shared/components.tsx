import { useId, useState, type ReactNode } from "react";
import { useTheme } from "./theme";

type Page = "status" | "review" | "logs";

export function AppHeader({
  eyebrow,
  title,
  description,
  current,
  action,
  reviewCount,
}: {
  eyebrow: ReactNode;
  title: string;
  description: ReactNode;
  current: Page;
  action?: ReactNode;
  reviewCount?: number;
}) {
  const { nextTheme, toggleTheme } = useTheme();
  const links: Array<[Page, string, string]> = [
    ["status", "Status", "/"],
    ["review", "Manual review", "/review"],
    ["logs", "Logs", "/logs"],
  ];
  return <header className="topbar">
    <div>
      <div className="eyebrow">{eyebrow}</div>
      <h1>{title}</h1>
      <p className="header-meta">{description}</p>
    </div>
    <div className="header-actions">
      <nav className="page-navigation" aria-label="Dashboard pages">
        {links.map(([page, label, href]) => <a
          className="btn btn-secondary"
          href={href}
          aria-current={current === page ? "page" : undefined}
          key={page}
        >{page === "review" && reviewCount != null ? `${label} (${reviewCount.toLocaleString()})` : label}</a>)}
      </nav>
      <button
        className="btn btn-secondary"
        type="button"
        onClick={toggleTheme}
        aria-label={`Switch to ${nextTheme} theme`}
      >{nextTheme === "light" ? "Light" : "Dark"} theme</button>
      {action}
    </div>
  </header>;
}

export function Panel({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`panel ${className}`.trim()}>{children}</section>;
}

export function PanelBody({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`panel-body ${className}`.trim()}>{children}</div>;
}

export function AdvancedFilters({ children, label = "More filters" }: { children: ReactNode; label?: string }) {
  const [open, setOpen] = useState(false);
  const contentId = useId();
  return <div className={`advanced-filters ${open ? "is-open" : ""}`}>
    <button
      className="advanced-filters-toggle"
      type="button"
      aria-expanded={open}
      aria-controls={contentId}
      onClick={() => setOpen((current) => !current)}
    >{label}</button>
    <div className="advanced-filter-grid" id={contentId}>{children}</div>
  </div>;
}

export function PageFooter({ review = false }: { review?: boolean }) {
  return <p className="footer-note">
    {review ? "Subtitle dialogue appears only in explicit cue comparisons." : "Trusted LAN endpoint · no subtitle text, hashes, credentials, or absolute paths exposed"}
  </p>;
}
