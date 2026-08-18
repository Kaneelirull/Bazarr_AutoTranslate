import type { ReactNode } from "react";
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

export function PageFooter() {
  return <p className="footer-note">
    Trusted LAN endpoint · no subtitle text, hashes, credentials, or absolute paths exposed
  </p>;
}
