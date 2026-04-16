const LOCALHOST_API_BASE_URL = "http://127.0.0.1:8000/api";
const LOCAL_DEV_FRONTEND_PORTS = new Set(["3000", "4173", "5173"]);

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

function isLocalHost(hostname: string): boolean {
  return hostname === "127.0.0.1" || hostname === "localhost";
}

function currentLocation(location?: Location): Location | undefined {
  if (location) {
    return location;
  }

  if (typeof window === "undefined") {
    return undefined;
  }

  return window.location;
}

function shouldUseWindowOrigin(location?: Location): boolean {
  if (!location) {
    return false;
  }

  if (!isLocalHost(location.hostname)) {
    return true;
  }

  return !LOCAL_DEV_FRONTEND_PORTS.has(location.port);
}

export function getApiBaseUrl(location?: Location): string {
  const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim();
  if (configuredBaseUrl) {
    return trimTrailingSlash(configuredBaseUrl);
  }

  const targetLocation = currentLocation(location);
  if (targetLocation && shouldUseWindowOrigin(targetLocation)) {
    return `${trimTrailingSlash(targetLocation.origin)}/api`;
  }

  return LOCALHOST_API_BASE_URL;
}

export function getWebSocketUrl(location?: Location): string {
  const apiBaseUrl = new URL(getApiBaseUrl(location));
  apiBaseUrl.protocol = apiBaseUrl.protocol === "https:" ? "wss:" : "ws:";
  apiBaseUrl.pathname = "/ws";
  apiBaseUrl.search = "";
  apiBaseUrl.hash = "";
  return apiBaseUrl.toString();
}
