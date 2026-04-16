import { describe, expect, it, vi } from "vitest";

import { formatCountdown, formatDurationBetween, formatPrice } from "./time";

describe("time helpers", () => {
  it("formats countdown with four segments", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-03-28T00:00:00Z"));
    expect(formatCountdown("2026-03-29T01:02:03Z")).toBe("01:01:02:03");
    vi.useRealTimers();
  });

  it("formats prices", () => {
    expect(formatPrice("123.4567")).toBe("123.4567");
  });

  it("formats elapsed durations between two timestamps", () => {
    expect(formatDurationBetween("2026-03-29T09:30:00Z", "2026-03-29T11:00:00Z")).toBe("1h 30m");
  });
});
