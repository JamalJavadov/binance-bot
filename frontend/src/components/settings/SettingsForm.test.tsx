import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { SettingsForm } from "./SettingsForm";
import { useAppStore } from "../../store/appStore";

const initialState = useAppStore.getState();

afterEach(() => {
  cleanup();
  useAppStore.setState(initialState, true);
});

describe("SettingsForm", () => {
  const aqrrSettings = {
    max_leverage: "10",
    risk_per_trade_pct: "2.0",
    max_portfolio_risk_pct: "6.0",
    deployable_equity_pct: "90",
    auto_mode_max_entry_drift_pct: "5.0",
    min_24h_quote_volume_usdt: "25000000",
    max_book_spread_bps: "12",
    kill_switch_consecutive_stop_losses: "2",
    kill_switch_daily_drawdown_pct: "4.0",
  };

  it("renders AQRR-only settings and submits updated values", () => {
    const onSave = vi.fn().mockResolvedValue(undefined);

    render(
      <StrictMode>
        <SettingsForm
          initialValues={aqrrSettings}
          onSave={onSave}
        />
      </StrictMode>,
    );

    expect(screen.getByLabelText("Deployable Equity %")).toBeInTheDocument();
    expect(screen.getByLabelText("Kill Switch Stop Losses")).toBeInTheDocument();
    expect(screen.getByLabelText("Min 24h Quote Volume")).toBeInTheDocument();
    expect(screen.queryByLabelText("Scan Top Symbols")).not.toBeInTheDocument();

    const leverageInput = screen.getByLabelText("Max Leverage");
    fireEvent.change(leverageInput, { target: { value: "8" } });
    fireEvent.submit(screen.getByRole("button", { name: "Save Settings" }).closest("form") as HTMLFormElement);

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        max_leverage: "8",
      }),
    );
  });

  it("renders and submits AQRR kill-switch settings", () => {
    const onSave = vi.fn().mockResolvedValue(undefined);

    render(
      <StrictMode>
        <SettingsForm
          initialValues={aqrrSettings}
          onSave={onSave}
        />
      </StrictMode>,
    );

    const killSwitchInput = screen.getByLabelText("Kill Switch Drawdown %");
    fireEvent.change(killSwitchInput, { target: { value: "3.5" } });
    fireEvent.submit(screen.getByRole("button", { name: "Save Settings" }).closest("form") as HTMLFormElement);

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        kill_switch_daily_drawdown_pct: "3.5",
      }),
    );
  });

  it("renders and submits the auto entry drift field", () => {
    const onSave = vi.fn().mockResolvedValue(undefined);

    render(
      <StrictMode>
        <SettingsForm
          initialValues={aqrrSettings}
          onSave={onSave}
        />
      </StrictMode>,
    );

    const driftInput = screen.getByLabelText("Auto Entry Drift %");
    fireEvent.change(driftInput, { target: { value: "4.5" } });
    fireEvent.submit(screen.getByRole("button", { name: "Save Settings" }).closest("form") as HTMLFormElement);

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        auto_mode_max_entry_drift_pct: "4.5",
      }),
    );
  });

});
