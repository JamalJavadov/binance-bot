import type { TriggerType } from "../types/api";

export function defaultStrategyLabel(triggerType?: TriggerType | null): string | null {
  return triggerType == null ? null : "AQRR Binance USD-M Strategy";
}

export function formatScanSource(triggerType?: TriggerType | null, strategyLabel?: string | null): string | null {
  if (triggerType == null) {
    return null;
  }
  const resolvedStrategy = strategyLabel ?? defaultStrategyLabel(triggerType);
  return resolvedStrategy ? `${triggerType} • ${resolvedStrategy}` : triggerType;
}
