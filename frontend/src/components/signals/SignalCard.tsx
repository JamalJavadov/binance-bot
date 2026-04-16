import { formatCountdown, formatPrice } from "../../lib/time";
import { useAppStore } from "../../store/appStore";
import type { OrderPreview, OrderPreviewStatus, RecommendedSignal, Signal } from "../../types/api";

type Props = {
  signal: Signal;
  recommendation?: RecommendedSignal;
  onApprove?: (signalId: number) => void;
  onDismiss?: (signalId: number) => void;
};

export function SignalCard({ signal, recommendation, onApprove, onDismiss }: Props) {
  const isApproving = useAppStore((state) => Boolean(state.pendingActions[`signal:approve:${signal.id}`]));
  const isDismissing = useAppStore((state) => Boolean(state.pendingActions[`signal:dismiss:${signal.id}`]));
  const preview =
    (recommendation?.live_readiness.order_preview as OrderPreview | undefined) ??
    (signal.extra_context?.order_preview as OrderPreview | undefined);
  const isLiveRecommendation = recommendation != null;
  const canOpenNow = recommendation?.live_readiness.can_open_now ?? true;
  const liveFailureReason = recommendation?.live_readiness.failure_reason ?? null;
  const liveMarkPrice = recommendation?.live_readiness.mark_price ?? null;
  const actionDisabled = isApproving || (isLiveRecommendation && !canOpenNow);
  const liveStateClass = !isLiveRecommendation ? "" : canOpenNow ? "live-ready" : "live-blocked";
  const liveStateText = !isLiveRecommendation ? null : canOpenNow ? "Ready for Binance" : "Blocked on Binance";

  const previewStatusText: Record<OrderPreviewStatus, string> = {
    affordable: "Fits live margin",
    resized_to_budget: "Will auto-resize",
    too_small_for_exchange: "Too small for Binance",
    not_affordable: "Not affordable now",
  };

  const affordabilityText = preview ? previewStatusText[preview.status] : "Preview unavailable";
  return (
    <article className={`card signal-card ${liveStateClass}`.trim()}>
      <div className="card-header">
        <div>
          {recommendation ? <p className="signal-rank">Best #{recommendation.rank}</p> : null}
          <h3>{signal.symbol}</h3>
          <p className="muted">{signal.direction}</p>
        </div>
        <div className="signal-card-meta">
          {liveStateText ? <div className={`live-status-chip ${liveStateClass}`}>{liveStateText}</div> : null}
          <div className="score-chip">Score {signal.final_score}</div>
        </div>
      </div>
      <div className="signal-grid">
        <div>
          <span className="label">Entry</span>
          <strong>{formatPrice(signal.entry_price)}</strong>
        </div>
        <div>
          <span className="label">SL</span>
          <strong>{formatPrice(signal.stop_loss)}</strong>
        </div>
        <div>
          <span className="label">TP</span>
          <strong>{formatPrice(signal.take_profit)}</strong>
        </div>
        <div>
          <span className="label">R:R</span>
          <strong>{signal.rr_ratio}</strong>
        </div>
        <div>
          <span className="label">Leverage</span>
          <strong>{preview ? `${preview.recommended_leverage}x` : "--"}</strong>
        </div>
        <div>
          <span className="label">Qty</span>
          <strong>{preview ? formatPrice(preview.final_quantity) : "--"}</strong>
        </div>
        <div>
          <span className="label">Margin Need</span>
          <strong>{preview ? `$${formatPrice(preview.required_initial_margin)}` : "--"}</strong>
        </div>
        <div>
          <span className="label">Budget</span>
          <strong>{preview ? `$${formatPrice(preview.risk_budget_usdt)}` : "--"}</strong>
        </div>
        <div>
          <span className="label">Mark at Signal</span>
          <strong>{signal.current_price_at_signal ? formatPrice(signal.current_price_at_signal) : "--"}</strong>
        </div>
        <div>
          <span className="label">{recommendation ? "Live Mark" : "Sizing Mark"}</span>
          <strong>{liveMarkPrice ? formatPrice(liveMarkPrice) : preview ? formatPrice(preview.mark_price_used) : "--"}</strong>
        </div>
      </div>
      <div className={`preview-chip ${preview?.status ?? "not_affordable"}`}>{affordabilityText}</div>
      {preview?.auto_resized ? (
        <p className="preview-note">
          Will place {formatPrice(preview.final_quantity)} instead of {formatPrice(preview.requested_quantity)} to fit live
          margin.
        </p>
      ) : null}
      {liveFailureReason ? <p className="live-reason">{liveFailureReason}</p> : null}
      {preview?.reason ? <p className="preview-note">{preview.reason}</p> : null}
      <p className="reason">{signal.reason_text ?? "No reason recorded"}</p>
      <div className="meta-row">
        <span>Expires in {formatCountdown(signal.expires_at)}</span>
        <span>{signal.status}</span>
      </div>
      <div className="actions">
        {onApprove ? (
          <button className={`primary-button ${liveStateClass}`.trim()} onClick={() => onApprove(signal.id)} disabled={actionDisabled}>
            {isApproving ? "Opening..." : "Open Order"}
          </button>
        ) : null}
        {onDismiss ? (
          <button className="secondary-button" onClick={() => onDismiss(signal.id)} disabled={isDismissing}>
            {isDismissing ? "Dismissing..." : "Dismiss"}
          </button>
        ) : null}
      </div>
    </article>
  );
}
