import { useEffect } from "react";

import { defaultStrategyLabel, formatScanSource } from "../../lib/scanLabels";
import { useAppStore } from "../../store/appStore";
import { ReadStatusNotice } from "../layout/ReadStatusNotice";
import { SignalCard } from "./SignalCard";

const RECOMMENDATIONS_POLL_MS = 15_000;

export function BestOrdersSection() {
  const recommendedSignals = useAppStore((state) => state.recommendedSignals);
  const recommendationsScanId = useAppStore((state) => state.recommendationsScanId);
  const recommendationsScanTriggerType = useAppStore((state) => state.recommendationsScanTriggerType);
  const recommendationsStrategyLabel = useAppStore((state) => state.recommendationsStrategyLabel);
  const recommendationsRefreshedAt = useAppStore((state) => state.recommendationsRefreshedAt);
  const refreshRecommendedSignals = useAppStore((state) => state.refreshRecommendedSignals);
  const recommendationsReadStatus = useAppStore((state) => state.readStates.recommendedSignals);

  useEffect(() => {
    void refreshRecommendedSignals();
    const timer = window.setInterval(() => {
      void refreshRecommendedSignals();
    }, RECOMMENDATIONS_POLL_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, [refreshRecommendedSignals]);

  const refreshedLabel = recommendationsRefreshedAt
    ? `Live Binance check updated ${new Date(recommendationsRefreshedAt).toLocaleTimeString()}.`
    : "Live Binance check refreshes every 15 seconds.";
  const latestSourceLabel = formatScanSource(
    recommendationsScanTriggerType,
    recommendationsStrategyLabel ?? defaultStrategyLabel(recommendationsScanTriggerType),
  );

  return (
    <section>
      <div className="section-head">
        <div className="section-head-copy">
          <h2>Top 3 Auto Mode Candidates</h2>
          <p className="muted">{refreshedLabel}</p>
          <p className="muted">Auto Mode ranks these setups for the next cycle and places live orders on its own.</p>
          {latestSourceLabel ? <p className="muted">Latest completed scan: {latestSourceLabel}</p> : null}
        </div>
      </div>
      <ReadStatusNotice
        status={recommendationsReadStatus}
        unavailableMessage="Auto Mode recommendations could not be refreshed from the backend."
        staleMessage="Showing the last successful recommendation snapshot because the latest backend refresh failed."
      />
      <div className="card-grid">
        {recommendedSignals.map((recommendation) => (
          <SignalCard key={recommendation.signal.id} signal={recommendation.signal} recommendation={recommendation} />
        ))}
        {recommendedSignals.length === 0 && !recommendationsReadStatus.error ? (
          <div className="card empty-state">
            {recommendationsScanId == null
              ? "No completed scan available yet."
              : "The latest completed scan did not produce any qualified Auto Mode candidates."}
          </div>
        ) : null}
      </div>
    </section>
  );
}
