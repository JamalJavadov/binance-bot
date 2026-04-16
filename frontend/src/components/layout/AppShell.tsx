import { type PropsWithChildren, useEffect } from "react";
import { NavLink } from "react-router-dom";

import { useAppStore } from "../../store/appStore";

const links = [
  { to: "/", label: "Dashboard" },
  { to: "/auto-mode", label: "Auto Mode" },
  { to: "/signals", label: "Signals" },
  { to: "/orders", label: "Orders" },
  { to: "/history", label: "History" },
  { to: "/settings", label: "Settings" },
  { to: "/api-credentials", label: "API Keys" },
];

export function AppShell({ children }: PropsWithChildren) {
  const bootstrap = useAppStore((state) => state.bootstrap);
  const connectSocket = useAppStore((state) => state.connectSocket);
  const disconnectSocket = useAppStore((state) => state.disconnectSocket);
  const balance = useAppStore((state) => state.balance);
  const status = useAppStore((state) => state.status);
  const feedback = useAppStore((state) => state.feedback);
  const clearFeedback = useAppStore((state) => state.clearFeedback);

  useEffect(() => {
    void bootstrap();
    connectSocket();

    return () => {
      disconnectSocket();
    };
  }, [bootstrap, connectSocket, disconnectSocket]);

  useEffect(() => {
    if (feedback?.kind !== "success") {
      return;
    }

    const timer = window.setTimeout(() => {
      clearFeedback();
    }, 4000);

    return () => {
      window.clearTimeout(timer);
    };
  }, [clearFeedback, feedback]);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div>
          <p className="eyebrow">LOCAL BINANCE FUTURES</p>
          <h1>Futures Bot</h1>
        </div>
        <nav className="nav" aria-label="Primary navigation">
          {links.map((link) => (
            <NavLink
              key={link.to}
              to={link.to}
              className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}
              end={link.to === "/"}
            >
              {link.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main className="main">
        <header className="header card">
          <div className="header-copy">
            <div className="status-line">
              <span className={`dot ${status?.backend_ok ? "ok" : "bad"}`} />
              {status?.backend_ok ? "Backend connected" : "Backend unavailable"}
            </div>
            <p className="muted">Binance reachable: {status?.binance_reachable ? "yes" : "no"}</p>
            {balance ? (
              <p className="muted">
                Available ${balance.available_balance.toFixed(2)} · Reserve ${balance.reserve_balance.toFixed(2)} · Wallet $
                {balance.balance.toFixed(2)}
              </p>
            ) : null}
          </div>
          <div className="balance-pill header-balance">
            <span>Usable</span>
            <strong>{balance ? `$${balance.usable_balance.toFixed(2)}` : "--"}</strong>
          </div>
        </header>
        {feedback ? (
          <div className={`feedback-banner ${feedback.kind}`} role={feedback.kind === "error" ? "alert" : "status"}>
            <p>{feedback.message}</p>
            <button
              className="feedback-dismiss"
              type="button"
              onClick={clearFeedback}
              aria-label="Dismiss notification"
            >
              Dismiss
            </button>
          </div>
        ) : null}
        {children}
      </main>
    </div>
  );
}
