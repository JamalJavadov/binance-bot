import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { CredentialsPage } from "./CredentialsPage";
import { useAppStore } from "../store/appStore";

const initialState = useAppStore.getState();

afterEach(() => {
  cleanup();
  useAppStore.setState(initialState, true);
});

describe("CredentialsPage", () => {
  it("keeps rendering the connection test card details", () => {
    useAppStore.setState({
      saveCredentials: vi.fn().mockResolvedValue(undefined),
      testCredentials: vi.fn().mockResolvedValue(undefined),
      credentials: {
        has_credentials: true,
        masked_api_key: "abcd...wxyz",
        last_updated: "2026-03-28T20:00:00Z",
      },
      connectionTest: {
        success: true,
        balance_usdt: 98.76,
        message: "Connected to Binance Futures",
      },
    });

    render(
      <StrictMode>
        <CredentialsPage />
      </StrictMode>,
    );

    expect(screen.getByText("Connection Test")).toBeInTheDocument();
    expect(screen.getByText("Connected to Binance Futures")).toBeInTheDocument();
    expect(screen.getByText("USDT balance: 98.76")).toBeInTheDocument();
  });
});
