import { BestOrdersSection } from "../components/signals/BestOrdersSection";
import { ReadStatusNotice } from "../components/layout/ReadStatusNotice";
import { defaultStrategyLabel, formatScanSource } from "../lib/scanLabels";
import { useAppStore } from "../store/appStore";
import { SignalCard } from "../components/signals/SignalCard";

export function SignalsPage() {
  const scanHistory = useAppStore((state) => state.scanHistory);
  const signals = useAppStore((state) => state.signals);
  const scanOverviewReadStatus = useAppStore((state) => state.readStates.scanOverview);
  const signalsReadStatus = useAppStore((state) => state.readStates.signals);
  const latestCompletedScan = scanHistory.find((cycle) => cycle.status === "COMPLETE") ?? null;
  const latestCompletedSignals = latestCompletedScan
    ? signals.filter((signal) => signal.scan_cycle_id === latestCompletedScan.id)
    : [];
  const latestStrategyLabel =
    latestCompletedSignals[0]?.strategy_label ?? defaultStrategyLabel(latestCompletedScan?.trigger_type);
  const latestSourceLabel = formatScanSource(latestCompletedScan?.trigger_type, latestStrategyLabel);

  return (
    <>
      <BestOrdersSection />
      <section>
        <div className="section-head">
          <div className="section-head-copy">
            <h2>Signals</h2>
            <p className="muted">
              {latestCompletedScan
                ? `${latestCompletedSignals.length} from the latest completed scan`
                : scanOverviewReadStatus.error
                  ? "Latest completed scan is unavailable right now."
                  : "No completed scan available yet."}
            </p>
          </div>
          {latestSourceLabel ? (
            <div className="section-head-meta">
              <p className="muted">Latest completed scan: {latestSourceLabel}</p>
            </div>
          ) : null}
        </div>
        <ReadStatusNotice
          status={scanOverviewReadStatus}
          unavailableMessage="Recent scan history could not be refreshed from the backend."
          staleMessage="Showing the last successful scan history snapshot because the latest backend refresh failed."
        />
        <p className="muted">Signals are read-only here. Auto Mode decides which qualified setup to place live.</p>
        <ReadStatusNotice
          status={signalsReadStatus}
          unavailableMessage="Signals could not be refreshed from the backend."
          staleMessage="Showing the last successful signals snapshot because the latest backend refresh failed."
        />
        <div className="card-grid">
          {latestCompletedSignals.map((signal) => <SignalCard key={signal.id} signal={signal} />)}
          {latestCompletedSignals.length === 0 && !signalsReadStatus.error && !scanOverviewReadStatus.error ? (
            <div className="card empty-state">
              {latestCompletedScan
                ? "The latest completed scan did not produce any stored signals."
                : "No completed scan available yet."}
            </div>
          ) : null}
        </div>
      </section>
    </>
  );
}
