import { StrictMode } from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { AppShell } from "./AppShell";
import { useAppStore } from "../../store/appStore";

const initialState = useAppStore.getState();

afterEach(() => {
  cleanup();
  useAppStore.setState(initialState, true);
});

describe("AppShell feedback banner", () => {
  it("renders feedback styles and replaces the previous banner with the latest action", () => {
    useAppStore.setState({
      bootstrap: vi.fn().mockResolvedValue(undefined),
      connectSocket: vi.fn(),
      feedback: undefined,
    });

    render(
      <StrictMode>
        <MemoryRouter>
          <AppShell>
            <div>Content</div>
          </AppShell>
        </MemoryRouter>
      </StrictMode>,
    );

    act(() => {
      useAppStore.setState({
        feedback: {
          kind: "success",
          message: "Settings saved",
          source: "settings:save",
        },
      });
    });

    const successBanner = screen.getByRole("status");
    expect(successBanner).toHaveClass("feedback-banner", "success");
    expect(screen.getByText("Settings saved")).toBeInTheDocument();

    act(() => {
      useAppStore.setState({
        feedback: {
          kind: "error",
          message: "Scan already running",
          source: "scan:start",
        },
      });
    });

    const errorBanner = screen.getByRole("alert");
    expect(errorBanner).toHaveClass("feedback-banner", "error");
    expect(screen.getByText("Scan already running")).toBeInTheDocument();
    expect(screen.queryByText("Settings saved")).not.toBeInTheDocument();
  });

  it("shows usable, available, reserve, and wallet balances in the header", () => {
    useAppStore.setState({
      bootstrap: vi.fn().mockResolvedValue(undefined),
      connectSocket: vi.fn(),
      feedback: undefined,
      balance: {
        asset: "USDT",
        balance: 16.72,
        available_balance: 16.72,
        usable_balance: 15.05,
        reserve_balance: 1.67,
      },
    });

    render(
      <StrictMode>
        <MemoryRouter>
          <AppShell>
            <div>Content</div>
          </AppShell>
        </MemoryRouter>
      </StrictMode>,
    );

    expect(screen.getByText("Usable")).toBeInTheDocument();
    expect(screen.getByText("$15.05")).toBeInTheDocument();
    expect(screen.getByText("Available $16.72 · Reserve $1.67 · Wallet $16.72")).toBeInTheDocument();
  });
});
