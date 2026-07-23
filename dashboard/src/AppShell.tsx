// AppShell: persistent left sidebar navigation + top bar + content area.
// The sidebar is the primary navigation for all sections; it collapses to
// icons on narrow viewports and becomes an overlay on mobile (<768px).

import type { ReactNode } from "react";
import { useState } from "react";

export interface NavItem {
  id: string;
  label: string;
  hash: string;
  icon: ReactNode;
  /** Section header above this item (e.g. "CONNECTORS", "ACCESS") */
  section?: string;
  /** Show a lock icon (admin-only indicator) */
  adminOnly?: boolean;
}

interface AppShellProps {
  navItems: NavItem[];
  activeId: string;
  email: string;
  isAdmin: boolean;
  onNavigate: (hash: string) => void;
  onSignOut: () => void;
  children: ReactNode;
}

export function AppShell({
  navItems,
  activeId,
  email,
  isAdmin,
  onNavigate,
  onSignOut,
  children,
}: AppShellProps) {
  const [mobileOpen, setMobileOpen] = useState(false);

  const handleNav = (hash: string) => {
    onNavigate(hash);
    setMobileOpen(false);
  };

  // Group items by section header
  let lastSection: string | undefined;

  return (
    <div className="app-shell">
      {/* Mobile overlay */}
      {mobileOpen && (
        <div className="sidebar-overlay" onClick={() => setMobileOpen(false)} />
      )}

      {/* Sidebar */}
      <aside className={`sidebar${mobileOpen ? " open" : ""}`}>
        <div className="sidebar-brand" onClick={() => handleNav("#/")}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="10" />
            <path d="M12 6v6l4 2" />
          </svg>
          <span className="sidebar-brand-text">SDLC Fleet</span>
        </div>

        <nav className="sidebar-nav">
          {navItems.map((item) => {
            const showSection = item.section && item.section !== lastSection;
            if (item.section) lastSection = item.section;
            return (
              <div key={item.id}>
                {showSection && (
                  <div className="nav-section-header">{item.section}</div>
                )}
                <button
                  className={`nav-item${activeId === item.id ? " active" : ""}`}
                  onClick={() => handleNav(item.hash)}
                  title={item.label}
                >
                  <span className="nav-icon">{item.icon}</span>
                  <span className="nav-label">{item.label}</span>
                  {item.adminOnly && (
                    <svg className="nav-lock" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <rect x="3" y="11" width="18" height="11" rx="2" />
                      <path d="M7 11V7a5 5 0 0110 0v4" />
                    </svg>
                  )}
                </button>
              </div>
            );
          })}
        </nav>
      </aside>

      {/* Main area */}
      <div className="main-area">
        {/* Top bar */}
        <header className="top-bar">
          <button
            className="mobile-menu-btn"
            onClick={() => setMobileOpen(!mobileOpen)}
            aria-label="Toggle navigation"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M3 12h18M3 6h18M3 18h18" />
            </svg>
          </button>
          <div className="top-bar-spacer" />
          <div className="top-bar-right">
            {isAdmin && <span className="role-badge">Admin</span>}
            <span className="top-bar-email">{email}</span>
            <button className="top-bar-signout" onClick={onSignOut}>Sign out</button>
          </div>
        </header>

        {/* Content */}
        <main className="page-content">
          {children}
        </main>
      </div>
    </div>
  );
}
