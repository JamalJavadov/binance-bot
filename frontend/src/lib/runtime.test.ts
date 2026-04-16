import { afterEach, describe, expect, it, vi } from "vitest";

import { getApiBaseUrl, getWebSocketUrl } from "./runtime";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("runtime config", () => {
  it("uses the configured API base URL and derives a secure websocket endpoint", () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://bot.example.com/api/");

    expect(getApiBaseUrl()).toBe("https://bot.example.com/api");
    expect(getWebSocketUrl()).toBe("wss://bot.example.com/ws");
  });

  it("falls back to the current origin outside local dev", () => {
    const location = new URL("https://tradebot.example.com/dashboard") as unknown as Location;

    expect(getApiBaseUrl(location)).toBe("https://tradebot.example.com/api");
    expect(getWebSocketUrl(location)).toBe("wss://tradebot.example.com/ws");
  });

  it("keeps the localhost backend fallback for local dev origins", () => {
    const location = new URL("http://localhost:3000/signals") as unknown as Location;

    expect(getApiBaseUrl(location)).toBe("http://127.0.0.1:8000/api");
    expect(getWebSocketUrl(location)).toBe("ws://127.0.0.1:8000/ws");
  });
});
