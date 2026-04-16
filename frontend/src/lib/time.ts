export function formatCountdown(targetIso: string): string {
  const remainingMs = Math.max(new Date(targetIso).getTime() - Date.now(), 0);
  const totalSeconds = Math.floor(remainingMs / 1000);
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor((totalSeconds % 86400) / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return [days, hours, minutes, seconds].map((value) => String(value).padStart(2, "0")).join(":");
}

export function formatPrice(value: string | number | undefined | null): string {
  if (value === undefined || value === null) {
    return "-";
  }
  return Number(value).toLocaleString(undefined, {
    maximumFractionDigits: 6,
    minimumFractionDigits: 2,
  });
}

export function formatDurationBetween(startIso?: string | null, endIso?: string | null): string {
  if (!startIso || !endIso) {
    return "--";
  }

  const durationMs = Math.max(new Date(endIso).getTime() - new Date(startIso).getTime(), 0);
  const totalSeconds = Math.floor(durationMs / 1000);
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor((totalSeconds % 86400) / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  if (days > 0) {
    return `${days}d ${hours}h ${minutes}m`;
  }
  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds}s`;
  }
  return `${seconds}s`;
}
