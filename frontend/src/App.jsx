import React, { useEffect, useState } from "react";
import {
  AudioLines,
  LayoutGrid,
  LogOut,
  Mail,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  ScrollText,
  Stethoscope,
  X,
} from "lucide-react";
import { api, getToken, setToken } from "./api.js";
import AudiblePanel from "./components/AudiblePanel.jsx";
import OutlookPanel from "./components/OutlookPanel.jsx";
import LogViewer from "./components/LogViewer.jsx";
import {
  Badge,
  Button,
  Card,
  Input,
  Spinner,
} from "./components/ui.jsx";

/* ---------------------------------------------------------------
   iftools — Verdana Health Design System
   Audible FP Checker only: Checker · Live Logs
   --------------------------------------------------------------- */

const NAV = [
  { id: "checker", label: "Audible FP", sub: "Forgot-password checker", icon: AudioLines },
  { id: "outlook", label: "Outlook",    sub: "OAuth2 bruter + inboxer", icon: Mail },
  { id: "logs",    label: "Live Logs",  sub: "Streaming job output",    icon: ScrollText },
];

export default function App() {
  const [auth, setAuth] = useState(null);
  const [tab, setTab] = useState("checker");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [running, setRunning] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [drawerOpen, setDrawerOpen] = useState(false);

  useEffect(() => {
    api
      .get("/api/config")
      .then((d) => setAuth({ needs: d.needs_auth }))
      .catch(() => setAuth({ needs: true }));
  }, []);
  useEffect(() => {
    if (auth?.needs && !getToken()) return undefined;
    const timer = setInterval(
      () =>
        api
          .get("/api/audible/status")
          .then((d) => setRunning(!!d.running))
          .catch(() => {}),
      2500,
    );
    return () => clearInterval(timer);
  }, [auth]);

  function openTab(id) {
    setTab(id);
    setDrawerOpen(false);
  }

  // Drawer is a mobile-only surface: close on Escape and when returning to desktop.
  useEffect(() => {
    if (!drawerOpen) return undefined;
    const onKeyDown = (e) => e.key === "Escape" && setDrawerOpen(false);
    const onResize = () => {
      if (window.innerWidth > 820) setDrawerOpen(false);
    };
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", onResize);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", onResize);
    };
  }, [drawerOpen]);

  async function doLogin() {
    try {
      const data = await api.post("/api/auth", { username, password });
      setToken(data.token);
      setAuth({ needs: data.needs_auth });
      setUsername("");
      setPassword("");
    } catch (error) {
      alert(`Login failed: ${error.message}`);
    }
  }

  if (auth === null)
    return (
      <main className="app-loading">
        <Spinner />
        <span>Loading application</span>
      </main>
    );
  if (auth.needs && !getToken())
    return (
      <main className="app-login">
        <Card className="app-login-card">
          <div className="app-login-mark">
            <Stethoscope size={26} />
          </div>
          <h1>iftools</h1>
          <p>Enter your credentials to open the console.</p>
          <div className="login-field">
            <label htmlFor="login-user" className="login-label">Username</label>
            <Input
              id="login-user"
              type="text"
              placeholder="Username"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && doLogin()}
            />
          </div>
          <div className="login-field">
            <label htmlFor="login-pass" className="login-label">Password</label>
            <Input
              id="login-pass"
              type="password"
              placeholder="Password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && doLogin()}
            />
          </div>
          <Button variant="primary" size="lg" onClick={doLogin}>
            Sign in
          </Button>
        </Card>
      </main>
    );

  const ActivePanel = {
    checker: AudiblePanel,
    outlook: OutlookPanel,
    logs: LogViewer,
  }[tab];

  return (
    <div
      className={`${sidebarOpen ? "app-shell" : "app-shell app-shell-collapsed"}${drawerOpen ? " app-drawer-open" : ""}`}
    >
      {/* Mobile top bar: brand + job status + labeled menu (not a bare hamburger). */}
      <header className="app-topbar">
        <div className="app-topbar-brand">
          <div className="app-brand-icon">
            <Stethoscope size={18} />
          </div>
          <strong>iftools</strong>
        </div>
        <div className="app-topbar-right">
          <Badge tone={running ? "success" : "muted"}>
            {running && <span className="pulse-dot" />}
            {running ? "Running" : "Idle"}
          </Badge>
          <Button
            variant="outline"
            className="app-menu-btn"
            onClick={() => setDrawerOpen(true)}
            aria-label="Open menu"
            aria-expanded={drawerOpen}
          >
            <Menu size={17} />
            <span>Menu</span>
          </Button>
        </div>
      </header>
      <div
        className="app-drawer-scrim"
        onClick={() => setDrawerOpen(false)}
        aria-hidden="true"
      />
      <aside
        className={
          sidebarOpen ? "app-sidebar" : "app-sidebar app-sidebar-collapsed"
        }
      >
        <div className="app-sidebar-top">
          <div className="app-brand">
            <div className="app-brand-icon">
              <Stethoscope size={20} />
            </div>
            <div className="app-brand-copy">
              <strong>iftools</strong>
              <a
                className="app-brand-link"
                href="https://jamurhiratake.com"
                target="_blank"
                rel="noreferrer"
              >
                Audible FP Checker
              </a>
            </div>
          </div>
          <button
            type="button"
            className="app-drawer-close"
            onClick={() => setDrawerOpen(false)}
            aria-label="Close menu"
          >
            <X size={18} />
          </button>
          <Button
            variant="ghost"
            size="sm"
            className="app-sidebar-toggle"
            onClick={() => setSidebarOpen((open) => !open)}
            aria-label={sidebarOpen ? "Collapse sidebar" : "Expand sidebar"}
            title={sidebarOpen ? "Collapse sidebar" : "Expand sidebar"}
          >
            {sidebarOpen ? (
              <PanelLeftClose size={17} />
            ) : (
              <PanelLeftOpen size={17} />
            )}
          </Button>
        </div>
        <nav className="app-nav">
          {NAV.map(({ id, label, sub, icon: Icon }) => (
            <button
              key={id}
              type="button"
              className={tab === id ? "app-nav-item active" : "app-nav-item"}
              onClick={() => openTab(id)}
              title={`${label} — ${sub}`}
            >
              <Icon size={17} />
              <span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="app-sidebar-footer">
          <Badge tone={running ? "success" : "muted"}>
            {running && <span className="pulse-dot" />}
            {running ? "Job running" : "Idle"}
          </Badge>
          {auth.needs && (
            <Button
              variant="ghost"
              size="sm"
              onClick={async () => {
                try {
                  await api.post("/api/logout", {});
                } catch {
                  /* expired token = already logged out, just clear local state */
                }
                setToken("");
                window.location.reload();
              }}
            >
              <LogOut size={14} /> <span>Sign out</span>
            </Button>
          )}
          <a
            className="app-support"
            href="https://jamurhiratake.com"
            target="_blank"
            rel="noreferrer"
            title="Hiratake Labs"
          >
            <LayoutGrid size={12} />
            <span>Hiratake Labs</span>
          </a>
        </div>
      </aside>
      <main className="app-main" key={tab}>
        <ActivePanel />
      </main>

      {/* Mobile bottom nav: the primary destinations, one thumb away. */}
      <nav className="app-bottomnav" aria-label="Primary">
        {NAV.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            className={
              tab === id ? "app-bottomnav-item active" : "app-bottomnav-item"
            }
            onClick={() => openTab(id)}
            aria-current={tab === id ? "page" : undefined}
          >
            <Icon size={19} />
            <span>{label}</span>
          </button>
        ))}
      </nav>
    </div>
  );
}
