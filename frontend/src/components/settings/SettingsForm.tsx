import { useEffect, useState } from "react";

import { useAppStore } from "../../store/appStore";

type Props = {
  initialValues: Record<string, string>;
  onSave: (values: Record<string, string>) => Promise<void>;
};

type SettingField = {
  key: string;
  label: string;
  type: "text" | "number";
  min?: number;
  max?: number;
  step?: number;
};

const settingFields: readonly SettingField[] = [
  { key: "max_leverage", label: "Max Leverage", type: "number", min: 1, max: 10 },
  { key: "risk_per_trade_pct", label: "Risk Per Trade %", type: "number", min: 0.1, max: 2, step: 0.1 },
  { key: "max_portfolio_risk_pct", label: "Max Portfolio Risk %", type: "number", min: 0.5, max: 10, step: 0.1 },
  { key: "deployable_equity_pct", label: "Deployable Equity %", type: "number", min: 50, max: 100, step: 1 },
  { key: "auto_mode_max_entry_drift_pct", label: "Auto Entry Drift %", type: "number", min: 0.5, max: 10, step: 0.1 },
  { key: "min_24h_quote_volume_usdt", label: "Min 24h Quote Volume", type: "text" },
  { key: "max_book_spread_bps", label: "Max Spread Bps", type: "number", min: 1, max: 100, step: 1 },
  { key: "kill_switch_consecutive_stop_losses", label: "Kill Switch Stop Losses", type: "number", min: 1, max: 10 },
  { key: "kill_switch_daily_drawdown_pct", label: "Kill Switch Drawdown %", type: "number", min: 1, max: 20, step: 0.5 },
];

export function SettingsForm({ initialValues, onSave }: Props) {
  const [values, setValues] = useState<Record<string, string>>(initialValues);
  const isSaving = useAppStore((state) => Boolean(state.pendingActions["settings:save"]));

  useEffect(() => setValues(initialValues), [initialValues]);

  return (
    <form
      className="settings-grid card"
      onSubmit={(event) => {
        event.preventDefault();
        if (isSaving) {
          return;
        }
        void onSave(values);
      }}
    >
      {settingFields.map((field) => (
        <label key={field.key} className="field">
          <span>{field.label}</span>
          <input
            type={field.type}
            min={field.type === "number" ? field.min : undefined}
            max={field.type === "number" ? field.max : undefined}
            step={field.type === "number" ? field.step : undefined}
            value={values[field.key] ?? ""}
            onChange={(event) => setValues({ ...values, [field.key]: event.target.value })}
          />
        </label>
      ))}
      <button className="primary-button" type="submit" disabled={isSaving}>
        {isSaving ? "Saving..." : "Save Settings"}
      </button>
    </form>
  );
}
