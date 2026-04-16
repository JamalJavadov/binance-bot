import { afterEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));

vi.mock("../api/client", () => ({
  api: apiMock,
}));

import { useAppStore } from "./appStore";

const initialState = useAppStore.getState();

function makeApiError(detail: string, message?: string) {
  return {
    isAxiosError: true,
    message: `Request failed with status code 400`,
    response: {
      data: { detail, message },
    },
  };
}

afterEach(() => {
  vi.clearAllMocks();
  useAppStore.setState(initialState, true);
});

describe("useAppStore mutation feedback", () => {
  it("reports settings save success and clears pending state", async () => {
    apiMock.patch.mockResolvedValue({ data: { settings: { max_leverage: "10" } } });

    const action = useAppStore.getState().updateSettings({ max_leverage: "10" });

    expect(useAppStore.getState().pendingActions["settings:save"]).toBe(true);

    await action;

    expect(useAppStore.getState().settings).toEqual({ max_leverage: "10" });
    expect(useAppStore.getState().feedback).toEqual({
      kind: "success",
      message: "Settings saved",
      source: "settings:save",
    });
    expect(useAppStore.getState().pendingActions).toEqual({});
  });

  it("reports Auto Mode start success and updates the runtime status", async () => {
    apiMock.patch.mockResolvedValue({
      data: {
        enabled: true,
        paused: false,
        running: false,
        signal_schedule: "15m_closed_candle",
        kill_switch_active: false,
        kill_switch_reason: null,
        active_order_count: 1,
        active_risk_usdt: "15.00",
        portfolio_risk_budget_usdt: "80.00",
        per_slot_risk_budget_usdt: "26.67",
        last_cycle_started_at: null,
        last_cycle_completed_at: null,
        next_cycle_at: null,
      },
    });

    const action = useAppStore.getState().setAutoModeEnabled(true);

    expect(useAppStore.getState().pendingActions["auto-mode:toggle"]).toBe(true);

    await action;

    expect(apiMock.patch).toHaveBeenCalledWith("/auto-mode", { enabled: true });
    expect(useAppStore.getState().autoMode?.enabled).toBe(true);
    expect(useAppStore.getState().feedback).toEqual({
      kind: "success",
      message: "Auto Mode started",
      source: "auto-mode:toggle",
    });
    expect(useAppStore.getState().pendingActions).toEqual({});
  });

  it("reports Auto Mode pause success and refreshes reconciled runtime data", async () => {
    const refreshStatus = vi.fn().mockResolvedValue(undefined);
    const refreshOrders = vi.fn().mockResolvedValue(undefined);
    const refreshAuditEntries = vi.fn().mockResolvedValue(undefined);
    useAppStore.setState({ refreshStatus, refreshOrders, refreshAuditEntries });
    apiMock.patch.mockResolvedValue({
      data: {
        enabled: true,
        paused: true,
        running: false,
        signal_schedule: "15m_closed_candle",
        kill_switch_active: false,
        kill_switch_reason: null,
        active_order_count: 1,
        active_risk_usdt: "15.00",
        portfolio_risk_budget_usdt: "80.00",
        per_slot_risk_budget_usdt: "26.67",
        last_cycle_started_at: null,
        last_cycle_completed_at: null,
        next_cycle_at: null,
      },
    });

    const action = useAppStore.getState().setAutoModePaused(true);

    expect(useAppStore.getState().pendingActions["auto-mode:pause"]).toBe(true);

    await action;

    expect(apiMock.patch).toHaveBeenCalledWith("/auto-mode", { paused: true });
    expect(refreshStatus).toHaveBeenCalledOnce();
    expect(refreshOrders).toHaveBeenCalledOnce();
    expect(refreshAuditEntries).toHaveBeenCalledOnce();
    expect(useAppStore.getState().autoMode?.paused).toBe(true);
    expect(useAppStore.getState().feedback).toEqual({
      kind: "success",
      message: "Auto Mode paused",
      source: "auto-mode:pause",
    });
    expect(useAppStore.getState().pendingActions).toEqual({});
  });

  it("reports credentials save success and clears pending state", async () => {
    const refreshCredentials = vi.fn().mockResolvedValue(undefined);
    useAppStore.setState({ refreshCredentials });
    apiMock.post.mockResolvedValue({ data: {} });

    const action = useAppStore.getState().saveCredentials({
      api_key: "key",
      public_key_pem: "public",
      private_key_pem: "private",
    });

    expect(useAppStore.getState().pendingActions["credentials:save"]).toBe(true);

    await action;

    expect(refreshCredentials).toHaveBeenCalledOnce();
    expect(useAppStore.getState().feedback).toEqual({
      kind: "success",
      message: "Credentials saved",
      source: "credentials:save",
    });
    expect(useAppStore.getState().pendingActions).toEqual({});
  });

  it("reports connection test success and keeps the test result", async () => {
    const refreshStatus = vi.fn().mockResolvedValue(undefined);
    useAppStore.setState({ refreshStatus });
    apiMock.get.mockResolvedValue({
      data: {
        success: true,
        balance_usdt: 321.45,
        message: "Connected to Binance Futures",
      },
    });

    const action = useAppStore.getState().testCredentials();

    expect(useAppStore.getState().pendingActions["credentials:test"]).toBe(true);

    await action;

    expect(refreshStatus).toHaveBeenCalledOnce();
    expect(useAppStore.getState().connectionTest).toEqual({
      success: true,
      balance_usdt: 321.45,
      message: "Connected to Binance Futures",
    });
    expect(useAppStore.getState().feedback).toEqual({
      kind: "success",
      message: "Connected to Binance Futures",
      source: "credentials:test",
    });
    expect(useAppStore.getState().pendingActions).toEqual({});
  });

  it("treats unsuccessful connection test responses as error feedback", async () => {
    const refreshStatus = vi.fn().mockResolvedValue(undefined);
    useAppStore.setState({ refreshStatus });
    apiMock.get.mockResolvedValue({
      data: {
        success: false,
        balance_usdt: null,
        message: "Invalid signature",
      },
    });

    const action = useAppStore.getState().testCredentials();

    expect(useAppStore.getState().pendingActions["credentials:test"]).toBe(true);

    await action;

    expect(refreshStatus).not.toHaveBeenCalled();
    expect(useAppStore.getState().connectionTest).toEqual({
      success: false,
      balance_usdt: null,
      message: "Invalid signature",
    });
    expect(useAppStore.getState().feedback).toEqual({
      kind: "error",
      message: "Invalid signature",
      source: "credentials:test",
    });
    expect(useAppStore.getState().pendingActions).toEqual({});
  });

  it("normalizes connection test request failures and clears pending state", async () => {
    apiMock.get.mockRejectedValue(makeApiError("Credentials not configured"));

    const action = useAppStore.getState().testCredentials();

    expect(useAppStore.getState().pendingActions["credentials:test"]).toBe(true);

    await action;

    expect(useAppStore.getState().connectionTest).toEqual({
      success: false,
      balance_usdt: null,
      message: "Credentials not configured",
    });
    expect(useAppStore.getState().feedback).toEqual({
      kind: "error",
      message: "Credentials not configured",
      source: "credentials:test",
    });
    expect(useAppStore.getState().pendingActions).toEqual({});
  });

  it("keeps the last successful read snapshot when an orders refresh fails", async () => {
    useAppStore.setState((state) => ({
      orders: [
        {
          id: 101,
          signal_id: 1,
          symbol: "BTCUSDT",
          direction: "LONG",
          leverage: 5,
          margin_type: "ISOLATED",
          entry_price: "100.00",
          stop_loss: "95.00",
          take_profit: "115.00",
          quantity: "1.00",
          position_margin: "20.00",
          notional_value: "100.00",
          rr_ratio: "3.0",
          entry_order_id: null,
          tp_order_id: null,
          sl_order_id: null,
          status: "ORDER_PLACED",
          placed_at: null,
          triggered_at: null,
          closed_at: null,
          cancelled_at: null,
          cancel_reason: null,
          expires_at: "2026-03-30T20:00:00Z",
          realized_pnl: null,
          close_price: null,
          close_type: null,
          risk_budget_usdt: "50.00",
          risk_usdt_at_stop: "10.00",
          risk_pct_of_wallet: "1.00",
          approved_by: "AUTO_MODE",
          created_at: "2026-03-28T20:00:00Z",
          updated_at: "2026-03-28T20:00:00Z",
        },
      ],
      readStates: {
        ...state.readStates,
        orders: {
          loaded: true,
          stale: false,
          error: undefined,
        },
      },
    }));
    apiMock.get.mockRejectedValue(makeApiError("Backend unavailable"));

    await useAppStore.getState().refreshOrders();

    expect(useAppStore.getState().orders).toHaveLength(1);
    expect(useAppStore.getState().orders[0].symbol).toBe("BTCUSDT");
    expect(useAppStore.getState().readStates.orders).toEqual({
      loaded: true,
      stale: true,
      error: "Backend unavailable",
    });
  });

  it("refreshes live recommendations on websocket scan and order events", async () => {
    vi.useFakeTimers();
    const refreshStatus = vi.fn().mockResolvedValue(undefined);
    const refreshScanOverview = vi.fn().mockResolvedValue(undefined);
    const refreshSignals = vi.fn().mockResolvedValue(undefined);
    const refreshRecommendedSignals = vi.fn().mockResolvedValue(undefined);
    const refreshOrders = vi.fn().mockResolvedValue(undefined);
    const refreshAutoModeStatus = vi.fn().mockResolvedValue(undefined);
    const refreshSelectedScanDetail = vi.fn().mockResolvedValue(undefined);
    const refreshLatestAutoModeScanDetail = vi.fn().mockResolvedValue(undefined);
    const refreshAuditEntries = vi.fn().mockResolvedValue(undefined);
    useAppStore.setState({
      refreshStatus,
      refreshScanOverview,
      refreshSignals,
      refreshRecommendedSignals,
      refreshOrders,
      refreshAutoModeStatus,
      refreshSelectedScanDetail,
      refreshLatestAutoModeScanDetail,
      refreshAuditEntries,
    });

    const sockets: Array<{
      url: string;
      readyState: number;
      onmessage?: (event: { data: string }) => void;
      onclose?: () => void;
      close: () => void;
    }> = [];
    const originalWebSocket = globalThis.WebSocket;
    globalThis.WebSocket = class {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSING = 2;
      static CLOSED = 3;

      url: string;
      readyState = 1;
      onmessage?: (event: { data: string }) => void;
      onclose?: () => void;

      constructor(url: string) {
        this.url = url;
        sockets.push(this);
      }

      close() {
        this.readyState = 3;
        this.onclose?.();
      }
    } as unknown as typeof WebSocket;

    try {
      useAppStore.getState().connectSocket();
      expect(sockets).toHaveLength(1);

      await sockets[0].onmessage?.({ data: JSON.stringify({ event: "scan_complete" }) });
      await sockets[0].onmessage?.({ data: JSON.stringify({ event: "order_status_change" }) });
      await sockets[0].onmessage?.({ data: JSON.stringify({ event: "auto_mode_state_change" }) });
      await vi.advanceTimersByTimeAsync(75);

      expect(refreshStatus).toHaveBeenCalledTimes(1);
      expect(refreshScanOverview).toHaveBeenCalledTimes(1);
      expect(refreshRecommendedSignals).toHaveBeenCalledTimes(1);
      expect(refreshOrders).toHaveBeenCalledTimes(1);
      expect(refreshAutoModeStatus).toHaveBeenCalledTimes(1);
      expect(refreshSelectedScanDetail).toHaveBeenCalledOnce();
      expect(refreshLatestAutoModeScanDetail).toHaveBeenCalledOnce();
      expect(refreshAuditEntries).toHaveBeenCalledTimes(1);
    } finally {
      globalThis.WebSocket = originalWebSocket;
      vi.useRealTimers();
    }
  });

  it("refreshes the latest Auto Mode scan detail independently from the dashboard-selected scan", async () => {
    useAppStore.setState({
      selectedScanId: 12,
      selectedScanDetail: {
        cycle: {
          id: 12,
          started_at: "2026-04-05T07:00:00Z",
          completed_at: "2026-04-05T07:05:00Z",
          status: "COMPLETE",
          symbols_scanned: 40,
          candidates_found: 2,
          signals_qualified: 1,
          trigger_type: "AUTO_MODE",
          error_message: null,
          progress_pct: 100,
        },
        detail_available: true,
        results: [],
        workflow: [],
      },
    });
    apiMock.get.mockImplementation((url: string) => {
      if (url === "/scan/status") {
        return Promise.resolve({
          data: {
            id: 388,
            started_at: "2026-04-05T07:23:14Z",
            completed_at: "2026-04-05T07:27:47Z",
            status: "COMPLETE",
            symbols_scanned: 300,
            candidates_found: 0,
            signals_qualified: 0,
            trigger_type: "AUTO_MODE",
            error_message: null,
            progress_pct: 100,
          },
        });
      }
      if (url === "/scan/388/detail") {
        return Promise.resolve({
          data: {
            cycle: {
              id: 388,
              started_at: "2026-04-05T07:23:14Z",
              completed_at: "2026-04-05T07:27:47Z",
              status: "COMPLETE",
              symbols_scanned: 300,
              candidates_found: 0,
              signals_qualified: 0,
              trigger_type: "AUTO_MODE",
              error_message: null,
              progress_pct: 100,
            },
            detail_available: true,
            results: [],
            workflow: [],
          },
        });
      }
      throw new Error(`Unexpected GET ${url}`);
    });

    await useAppStore.getState().refreshLatestAutoModeScanDetail();

    expect(useAppStore.getState().selectedScanId).toBe(12);
    expect(useAppStore.getState().selectedScanDetail?.cycle.id).toBe(12);
    expect(useAppStore.getState().latestAutoModeScanDetail?.cycle.id).toBe(388);
  });

  it("deduplicates concurrent bootstrap calls", async () => {
    let resolveStatusRefresh: (() => void) | undefined;
    const refreshStatus = vi.fn().mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveStatusRefresh = resolve;
        }),
    );
    const refreshSignals = vi.fn().mockResolvedValue(undefined);
    const refreshRecommendedSignals = vi.fn().mockResolvedValue(undefined);
    const refreshOrders = vi.fn().mockResolvedValue(undefined);
    const refreshAutoModeStatus = vi.fn().mockResolvedValue(undefined);
    const refreshScanOverview = vi.fn().mockResolvedValue(undefined);
    const refreshLatestAutoModeScanDetail = vi.fn().mockResolvedValue(undefined);
    const refreshSettings = vi.fn().mockResolvedValue(undefined);
    const refreshCredentials = vi.fn().mockResolvedValue(undefined);
    const refreshAuditEntries = vi.fn().mockResolvedValue(undefined);

    useAppStore.setState({
      refreshStatus,
      refreshSignals,
      refreshRecommendedSignals,
      refreshOrders,
      refreshAutoModeStatus,
      refreshScanOverview,
      refreshLatestAutoModeScanDetail,
      refreshSettings,
      refreshCredentials,
      refreshAuditEntries,
    });

    const firstBootstrap = useAppStore.getState().bootstrap();
    const secondBootstrap = useAppStore.getState().bootstrap();

    expect(refreshStatus).toHaveBeenCalledOnce();
    expect(refreshSignals).toHaveBeenCalledOnce();
    expect(useAppStore.getState().loading).toBe(true);

    resolveStatusRefresh?.();
    await Promise.all([firstBootstrap, secondBootstrap]);

    expect(refreshRecommendedSignals).toHaveBeenCalledOnce();
    expect(refreshOrders).toHaveBeenCalledOnce();
    expect(refreshLatestAutoModeScanDetail).toHaveBeenCalledOnce();
    expect(refreshAuditEntries).toHaveBeenCalledOnce();
    expect(useAppStore.getState().loading).toBe(false);
    expect(useAppStore.getState().bootstrapPromise).toBeUndefined();
  });

  it("avoids duplicate websocket connections and reconnects after unexpected closes", async () => {
    vi.useFakeTimers();
    vi.stubEnv("VITE_API_BASE_URL", "https://bot.example.com/api");

    const sockets: Array<{
      url: string;
      readyState: number;
      onclose?: () => void;
      close: () => void;
    }> = [];
    const originalWebSocket = globalThis.WebSocket;
    globalThis.WebSocket = class {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSING = 2;
      static CLOSED = 3;

      url: string;
      readyState = 1;
      onclose?: () => void;

      constructor(url: string) {
        this.url = url;
        sockets.push(this);
      }

      close() {
        this.readyState = 3;
        this.onclose?.();
      }
    } as unknown as typeof WebSocket;

    try {
      useAppStore.getState().connectSocket();
      useAppStore.getState().connectSocket();

      expect(sockets).toHaveLength(1);
      expect(sockets[0].url).toBe("wss://bot.example.com/ws");

      sockets[0].readyState = 3;
      sockets[0].onclose?.();

      await vi.advanceTimersByTimeAsync(1000);

      expect(sockets).toHaveLength(2);
      expect(useAppStore.getState().socketReconnectAttempts).toBe(1);

      useAppStore.getState().disconnectSocket();
      await vi.runOnlyPendingTimersAsync();

      expect(sockets).toHaveLength(2);
    } finally {
      globalThis.WebSocket = originalWebSocket;
      vi.useRealTimers();
      vi.unstubAllEnvs();
    }
  });
});
