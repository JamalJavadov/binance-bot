import { create } from "zustand";

import { api } from "../api/client";
import { getApiErrorMessage } from "../api/errors";
import { getWebSocketUrl } from "../lib/runtime";
import type {
  AutoModeStatus,
  AuditEntry,
  BalanceResponse,
  ConnectionTestResponse,
  CredentialsStatus,
  HealthStatus,
  Order,
  PortfolioSummary,
  Position,
  RecommendedSignal,
  ScanCycleDetail,
  ScanCycle,
  SignalRecommendationsResponse,
  SettingsResponse,
  Signal,
} from "../types/api";
import type { ActionFeedback, ReadStatus } from "../types/ui";

type ApiCredentialsPayload = {
  api_key: string;
  public_key_pem: string;
  private_key_pem: string;
};

type RefreshActionKey =
  | "refreshAuditEntries"
  | "refreshSignals"
  | "refreshRecommendedSignals"
  | "refreshOrders"
  | "refreshAutoModeStatus"
  | "refreshStatus"
  | "refreshScanOverview"
  | "refreshSelectedScanDetail"
  | "refreshLatestAutoModeScanDetail"
  | "refreshSettings"
  | "refreshCredentials";

let latestAutoModeScanDetailRequestId = 0;

const SOCKET_EVENT_REFRESH_DELAY_MS = 75;

type ReadResourceKey =
  | "statusOverview"
  | "scanOverview"
  | "selectedScanDetail"
  | "latestAutoModeScanDetail"
  | "signals"
  | "recommendedSignals"
  | "orders"
  | "autoMode"
  | "settings"
  | "credentials"
  | "auditEntries";

type ReadStateMap = Record<ReadResourceKey, ReadStatus>;

type ReadResult<T> =
  | {
      ok: true;
      data: T;
    }
  | {
      ok: false;
      error: string;
    };

const SOCKET_EVENT_REFRESH_MAP: Record<string, readonly RefreshActionKey[]> = {
  scan_progress: ["refreshScanOverview", "refreshAuditEntries"],
  scan_complete: [
    "refreshScanOverview",
    "refreshRecommendedSignals",
    "refreshAutoModeStatus",
    "refreshLatestAutoModeScanDetail",
    "refreshAuditEntries",
  ],
  signal_found: ["refreshSignals", "refreshRecommendedSignals", "refreshSelectedScanDetail", "refreshAuditEntries"],
  order_status_change: [
    "refreshStatus",
    "refreshOrders",
    "refreshAutoModeStatus",
    "refreshRecommendedSignals",
    "refreshSelectedScanDetail",
    "refreshAuditEntries",
  ],
  auto_mode_state_change: [
    "refreshStatus",
    "refreshOrders",
    "refreshAutoModeStatus",
    "refreshScanOverview",
    "refreshRecommendedSignals",
    "refreshAuditEntries",
  ],
};

const SOCKET_REFRESH_SEQUENCE: readonly RefreshActionKey[] = [
  "refreshStatus",
  "refreshOrders",
  "refreshAutoModeStatus",
  "refreshScanOverview",
  "refreshLatestAutoModeScanDetail",
  "refreshSignals",
  "refreshRecommendedSignals",
  "refreshSelectedScanDetail",
  "refreshAuditEntries",
];

function initialReadStatus(): ReadStatus {
  return {
    loaded: false,
    stale: false,
    error: undefined,
  };
}

function initialReadStates(): ReadStateMap {
  return {
    statusOverview: initialReadStatus(),
    scanOverview: initialReadStatus(),
    selectedScanDetail: initialReadStatus(),
    latestAutoModeScanDetail: initialReadStatus(),
    signals: initialReadStatus(),
    recommendedSignals: initialReadStatus(),
    orders: initialReadStatus(),
    autoMode: initialReadStatus(),
    settings: initialReadStatus(),
    credentials: initialReadStatus(),
    auditEntries: initialReadStatus(),
  };
}

let queuedSocketEvents = new Set<string>();
let socketRefreshTimer: number | undefined;

function updateReadSuccess(readStates: ReadStateMap, key: ReadResourceKey): ReadStateMap {
  return {
    ...readStates,
    [key]: {
      loaded: true,
      stale: false,
      error: undefined,
    },
  };
}

function updateReadFailure(readStates: ReadStateMap, key: ReadResourceKey, error: string): ReadStateMap {
  const previous = readStates[key];
  return {
    ...readStates,
    [key]: {
      loaded: previous.loaded,
      stale: previous.loaded,
      error,
    },
  };
}

function resetReadState(readStates: ReadStateMap, key: ReadResourceKey): ReadStateMap {
  return {
    ...readStates,
    [key]: initialReadStatus(),
  };
}

async function readGet<T>(url: string): Promise<ReadResult<T>> {
  try {
    const response = await api.get<T>(url);
    return {
      ok: true,
      data: response.data,
    };
  } catch (error) {
    return {
      ok: false,
      error: getApiErrorMessage(error),
    };
  }
}

function queuedSocketActions(): RefreshActionKey[] {
  const requestedActions = new Set<RefreshActionKey>();
  for (const eventName of queuedSocketEvents) {
    for (const action of SOCKET_EVENT_REFRESH_MAP[eventName] ?? []) {
      requestedActions.add(action);
    }
  }
  return SOCKET_REFRESH_SEQUENCE.filter((action) => requestedActions.has(action));
}

function clearQueuedSocketRefreshTimer(): void {
  if (typeof window === "undefined" || socketRefreshTimer == null) {
    return;
  }
  window.clearTimeout(socketRefreshTimer);
  socketRefreshTimer = undefined;
}

async function flushQueuedSocketEvents(get: () => AppState): Promise<void> {
  clearQueuedSocketRefreshTimer();
  const actions = queuedSocketActions();
  queuedSocketEvents = new Set();

  for (const action of actions) {
    await get()[action]();
  }
}

function scheduleSocketEventRefresh(get: () => AppState, eventName: string): void {
  if (!(eventName in SOCKET_EVENT_REFRESH_MAP) || typeof window === "undefined") {
    return;
  }

  queuedSocketEvents.add(eventName);
  if (socketRefreshTimer != null) {
    return;
  }

  socketRefreshTimer = window.setTimeout(() => {
    void flushQueuedSocketEvents(get);
  }, SOCKET_EVENT_REFRESH_DELAY_MS);
}

type AppState = {
  status?: HealthStatus;
  balance?: BalanceResponse;
  portfolioSummary?: PortfolioSummary;
  positions: Position[];
  latestScan?: ScanCycle | null;
  scanHistory: ScanCycle[];
  selectedScanId?: number | null;
  selectedScanDetail?: ScanCycleDetail | null;
  latestAutoModeScanDetail?: ScanCycleDetail | null;
  signals: Signal[];
  recommendedSignals: RecommendedSignal[];
  recommendationsScanId?: number | null;
  recommendationsScanTriggerType?: SignalRecommendationsResponse["latest_completed_scan_trigger_type"];
  recommendationsStrategyKey?: string | null;
  recommendationsStrategyLabel?: string | null;
  recommendationsRefreshedAt?: string;
  orders: Order[];
  autoMode?: AutoModeStatus;
  settings: Record<string, string>;
  credentials?: CredentialsStatus;
  connectionTest?: ConnectionTestResponse;
  auditEntries: AuditEntry[];
  readStates: ReadStateMap;
  feedback?: ActionFeedback;
  pendingActions: Record<string, boolean>;
  loading: boolean;
  error?: string;
  socket?: WebSocket;
  socketReconnectTimer?: number;
  socketReconnectAttempts: number;
  socketManuallyClosed: boolean;
  bootstrapPromise?: Promise<void>;
  bootstrap: () => Promise<void>;
  refreshAuditEntries: () => Promise<void>;
  refreshSignals: () => Promise<void>;
  refreshRecommendedSignals: () => Promise<void>;
  refreshOrders: () => Promise<void>;
  refreshAutoModeStatus: () => Promise<void>;
  refreshStatus: () => Promise<void>;
  refreshScanOverview: () => Promise<void>;
  refreshSelectedScanDetail: (scanId?: number | null) => Promise<void>;
  refreshLatestAutoModeScanDetail: () => Promise<void>;
  refreshSettings: () => Promise<void>;
  refreshCredentials: () => Promise<void>;
  selectScan: (scanId: number) => Promise<void>;
  updateSettings: (values: Record<string, string>) => Promise<void>;
  setAutoModeEnabled: (enabled: boolean) => Promise<void>;
  setAutoModePaused: (paused: boolean) => Promise<void>;
  saveCredentials: (payload: ApiCredentialsPayload) => Promise<void>;
  testCredentials: () => Promise<void>;
  clearFeedback: () => void;
  connectSocket: () => void;
  disconnectSocket: () => void;
};

export const useAppStore = create<AppState>((set, get) => ({
  feedback: undefined,
  pendingActions: {},
  positions: [],
  scanHistory: [],
  selectedScanId: null,
  selectedScanDetail: null,
  latestAutoModeScanDetail: null,
  signals: [],
  recommendedSignals: [],
  recommendationsScanId: null,
  recommendationsScanTriggerType: null,
  recommendationsStrategyKey: null,
  recommendationsStrategyLabel: null,
  recommendationsRefreshedAt: undefined,
  orders: [],
  autoMode: undefined,
  settings: {},
  auditEntries: [],
  readStates: initialReadStates(),
  loading: false,
  socketReconnectAttempts: 0,
  socketManuallyClosed: false,

  bootstrap: async () => {
    const pendingBootstrap = get().bootstrapPromise;
    if (pendingBootstrap) {
      await pendingBootstrap;
      return;
    }

    const bootstrapPromise = (async () => {
      set({ loading: true, error: undefined });
      try {
        await Promise.all([
          get().refreshStatus(),
          get().refreshSignals(),
          get().refreshRecommendedSignals(),
          get().refreshOrders(),
          get().refreshAutoModeStatus(),
          get().refreshScanOverview(),
          get().refreshLatestAutoModeScanDetail(),
          get().refreshSettings(),
          get().refreshCredentials(),
          get().refreshAuditEntries(),
        ]);
      } finally {
        set({ loading: false, bootstrapPromise: undefined });
      }
    })();

    set({ bootstrapPromise });
    await bootstrapPromise;
  },

  clearFeedback: () => {
    set({ feedback: undefined });
  },

  refreshAuditEntries: async () => {
    const result = await readGet<AuditEntry[]>("/history");
    if (result.ok) {
      set((state) => ({
        auditEntries: result.data,
        readStates: updateReadSuccess(state.readStates, "auditEntries"),
      }));
      return;
    }

    set((state) => ({
      readStates: updateReadFailure(state.readStates, "auditEntries", result.error),
    }));
  },

  refreshStatus: async () => {
    const [status, balance, portfolioSummary, positions] = await Promise.all([
      readGet<HealthStatus>("/status"),
      readGet<BalanceResponse>("/account/balance"),
      readGet<PortfolioSummary>("/account/portfolio-summary"),
      readGet<Position[]>("/account/positions"),
    ]);

    if (status.ok && balance.ok && portfolioSummary.ok && positions.ok) {
      set((state) => ({
        status: status.data,
        balance: balance.data,
        portfolioSummary: portfolioSummary.data,
        positions: positions.data,
        readStates: updateReadSuccess(state.readStates, "statusOverview"),
      }));
      return;
    }

    const firstError = [status, balance, portfolioSummary, positions].find((result) => !result.ok);
    set((state) => ({
      readStates: updateReadFailure(
        state.readStates,
        "statusOverview",
        firstError?.ok ? "Status refresh failed." : firstError?.error ?? "Status refresh failed.",
      ),
    }));
  },

  refreshScanOverview: async () => {
    const previousLatestId = get().latestScan?.id ?? null;
    const previousSelectedId = get().selectedScanId ?? null;
    const [latestScan, scanHistory] = await Promise.all([
      readGet<ScanCycle | null>("/scan/status"),
      readGet<ScanCycle[]>("/scan/history"),
    ]);

    if (!latestScan.ok || !scanHistory.ok) {
      const failedResult = latestScan.ok ? scanHistory : latestScan;
      set((state) => ({
        readStates: updateReadFailure(
          state.readStates,
          "scanOverview",
          failedResult.ok ? "Scan overview refresh failed." : failedResult.error,
        ),
      }));
      return;
    }

    const nextLatestScan = latestScan.data ?? null;
    const nextScanHistory = scanHistory.data;
    let nextSelectedScanId = previousSelectedId;

    if (nextSelectedScanId == null) {
      nextSelectedScanId = nextLatestScan?.id ?? null;
    } else if (!nextScanHistory.some((cycle) => cycle.id === nextSelectedScanId)) {
      nextSelectedScanId = nextLatestScan?.id ?? null;
    } else if (previousSelectedId === previousLatestId && nextLatestScan != null) {
      nextSelectedScanId = nextLatestScan.id;
    }

    set((state) => ({
      latestScan: nextLatestScan,
      scanHistory: nextScanHistory,
      selectedScanId: nextSelectedScanId,
      readStates: updateReadSuccess(state.readStates, "scanOverview"),
    }));

    if (nextSelectedScanId == null) {
      set((state) => ({
        selectedScanDetail: null,
        readStates: resetReadState(state.readStates, "selectedScanDetail"),
      }));
      return;
    }

    await get().refreshSelectedScanDetail(nextSelectedScanId);
  },

  refreshSelectedScanDetail: async (scanId) => {
    const targetScanId = scanId ?? get().selectedScanId ?? null;
    if (targetScanId == null) {
      set((state) => ({
        selectedScanDetail: null,
        readStates: resetReadState(state.readStates, "selectedScanDetail"),
      }));
      return;
    }

    const detail = await readGet<ScanCycleDetail>(`/scan/${targetScanId}/detail`);
    if (get().selectedScanId !== targetScanId) {
      return;
    }

    if (detail.ok) {
      set((state) => ({
        selectedScanDetail: detail.data,
        readStates: updateReadSuccess(state.readStates, "selectedScanDetail"),
      }));
      return;
    }

    set((state) => ({
      readStates: updateReadFailure(state.readStates, "selectedScanDetail", detail.error),
    }));
  },

  refreshLatestAutoModeScanDetail: async () => {
    const requestId = ++latestAutoModeScanDetailRequestId;
    const currentLatestScan = get().latestScan ?? null;
    let targetScanId = currentLatestScan?.id ?? null;

    if (targetScanId == null) {
      const latestScan = await readGet<ScanCycle | null>("/scan/status");
      if (!latestScan.ok) {
        if (requestId !== latestAutoModeScanDetailRequestId) {
          return;
        }

        set((state) => ({
          readStates: updateReadFailure(state.readStates, "latestAutoModeScanDetail", latestScan.error),
        }));
        return;
      }

      targetScanId = latestScan.data?.id ?? null;
    }

    if (requestId !== latestAutoModeScanDetailRequestId) {
      return;
    }

    if (targetScanId == null) {
      set((state) => ({
        latestAutoModeScanDetail: null,
        readStates: updateReadSuccess(state.readStates, "latestAutoModeScanDetail"),
      }));
      return;
    }

    const detail = await readGet<ScanCycleDetail>(`/scan/${targetScanId}/detail`);
    if (requestId !== latestAutoModeScanDetailRequestId) {
      return;
    }

    if (detail.ok) {
      set((state) => ({
        latestAutoModeScanDetail: detail.data,
        readStates: updateReadSuccess(state.readStates, "latestAutoModeScanDetail"),
      }));
      return;
    }

    set((state) => ({
      readStates: updateReadFailure(state.readStates, "latestAutoModeScanDetail", detail.error),
    }));
  },

  refreshSignals: async () => {
    const result = await readGet<Signal[]>("/signals");
    if (result.ok) {
      set((state) => ({
        signals: result.data,
        readStates: updateReadSuccess(state.readStates, "signals"),
      }));
      return;
    }

    set((state) => ({
      readStates: updateReadFailure(state.readStates, "signals", result.error),
    }));
  },

  refreshRecommendedSignals: async () => {
    const result = await readGet<SignalRecommendationsResponse>("/signals/recommendations");
    if (result.ok) {
      set((state) => ({
        recommendedSignals: result.data.items,
        recommendationsScanId: result.data.latest_completed_scan_id ?? null,
        recommendationsScanTriggerType: result.data.latest_completed_scan_trigger_type ?? null,
        recommendationsStrategyKey: result.data.latest_completed_scan_strategy_key ?? null,
        recommendationsStrategyLabel: result.data.latest_completed_scan_strategy_label ?? null,
        recommendationsRefreshedAt: result.data.refreshed_at,
        readStates: updateReadSuccess(state.readStates, "recommendedSignals"),
      }));
      return;
    }

    set((state) => ({
      readStates: updateReadFailure(state.readStates, "recommendedSignals", result.error),
    }));
  },

  refreshOrders: async () => {
    const result = await readGet<Order[]>("/orders");
    if (result.ok) {
      set((state) => ({
        orders: result.data,
        readStates: updateReadSuccess(state.readStates, "orders"),
      }));
      return;
    }

    set((state) => ({
      readStates: updateReadFailure(state.readStates, "orders", result.error),
    }));
  },

  refreshAutoModeStatus: async () => {
    const result = await readGet<AutoModeStatus>("/auto-mode");
    if (result.ok) {
      set((state) => ({
        autoMode: result.data,
        readStates: updateReadSuccess(state.readStates, "autoMode"),
      }));
      return;
    }

    set((state) => ({
      readStates: updateReadFailure(state.readStates, "autoMode", result.error),
    }));
  },

  refreshSettings: async () => {
    const result = await readGet<SettingsResponse>("/settings");
    if (result.ok) {
      set((state) => ({
        settings: result.data.settings,
        readStates: updateReadSuccess(state.readStates, "settings"),
      }));
      return;
    }

    set((state) => ({
      readStates: updateReadFailure(state.readStates, "settings", result.error),
    }));
  },

  refreshCredentials: async () => {
    const result = await readGet<CredentialsStatus>("/credentials");
    if (result.ok) {
      set((state) => ({
        credentials: result.data,
        readStates: updateReadSuccess(state.readStates, "credentials"),
      }));
      return;
    }

    set((state) => ({
      readStates: updateReadFailure(state.readStates, "credentials", result.error),
    }));
  },

  selectScan: async (scanId: number) => {
    set((state) => ({
      selectedScanId: scanId,
      selectedScanDetail: null,
      readStates: resetReadState(state.readStates, "selectedScanDetail"),
    }));
    await get().refreshSelectedScanDetail(scanId);
  },

  updateSettings: async (values: Record<string, string>) => {
    const actionKey = "settings:save";
    if (get().pendingActions[actionKey]) {
      return;
    }

    set((state) => ({
      pendingActions: { ...state.pendingActions, [actionKey]: true },
    }));

    try {
      const response = await api.patch<SettingsResponse>("/settings", { values });
      set({
        settings: response.data.settings,
        feedback: {
          kind: "success",
          message: "Settings saved",
          source: actionKey,
        },
      });
    } catch (error) {
      set({
        feedback: {
          kind: "error",
          message: getApiErrorMessage(error),
          source: actionKey,
        },
      });
    } finally {
      set((state) => {
        const pendingActions = { ...state.pendingActions };
        delete pendingActions[actionKey];
        return { pendingActions };
      });
    }
  },

  setAutoModeEnabled: async (enabled: boolean) => {
    const actionKey = "auto-mode:toggle";
    if (get().pendingActions[actionKey]) {
      return;
    }

    set((state) => ({
      pendingActions: { ...state.pendingActions, [actionKey]: true },
    }));

    try {
      const response = await api.patch<AutoModeStatus>("/auto-mode", { enabled });
      set((state) => ({
        autoMode: response.data,
        readStates: updateReadSuccess(state.readStates, "autoMode"),
        feedback: {
          kind: "success",
          message: enabled ? "Auto Mode started" : "Auto Mode stopped",
          source: actionKey,
        },
      }));
    } catch (error) {
      set({
        feedback: {
          kind: "error",
          message: getApiErrorMessage(error),
          source: actionKey,
        },
      });
    } finally {
      set((state) => {
        const pendingActions = { ...state.pendingActions };
        delete pendingActions[actionKey];
        return { pendingActions };
      });
    }
  },

  setAutoModePaused: async (paused: boolean) => {
    const actionKey = "auto-mode:pause";
    if (get().pendingActions[actionKey]) {
      return;
    }

    set((state) => ({
      pendingActions: { ...state.pendingActions, [actionKey]: true },
    }));

    try {
      const response = await api.patch<AutoModeStatus>("/auto-mode", { paused });
      await Promise.all([get().refreshStatus(), get().refreshOrders(), get().refreshAuditEntries()]);
      set((state) => ({
        autoMode: response.data,
        readStates: updateReadSuccess(state.readStates, "autoMode"),
        feedback: {
          kind: "success",
          message: paused ? "Auto Mode paused" : "Auto Mode resumed",
          source: actionKey,
        },
      }));
    } catch (error) {
      set({
        feedback: {
          kind: "error",
          message: getApiErrorMessage(error),
          source: actionKey,
        },
      });
    } finally {
      set((state) => {
        const pendingActions = { ...state.pendingActions };
        delete pendingActions[actionKey];
        return { pendingActions };
      });
    }
  },

  saveCredentials: async (payload: ApiCredentialsPayload) => {
    const actionKey = "credentials:save";
    if (get().pendingActions[actionKey]) {
      return;
    }

    set((state) => ({
      pendingActions: { ...state.pendingActions, [actionKey]: true },
    }));

    try {
      await api.post("/credentials", payload);
      await get().refreshCredentials();
      set({
        feedback: {
          kind: "success",
          message: "Credentials saved",
          source: actionKey,
        },
      });
    } catch (error) {
      set({
        feedback: {
          kind: "error",
          message: getApiErrorMessage(error),
          source: actionKey,
        },
      });
    } finally {
      set((state) => {
        const pendingActions = { ...state.pendingActions };
        delete pendingActions[actionKey];
        return { pendingActions };
      });
    }
  },

  testCredentials: async () => {
    const actionKey = "credentials:test";
    if (get().pendingActions[actionKey]) {
      return;
    }

    set((state) => ({
      pendingActions: { ...state.pendingActions, [actionKey]: true },
    }));

    try {
      const response = await api.get<ConnectionTestResponse>("/credentials/test");
      set({ connectionTest: response.data });

      if (response.data.success) {
        await get().refreshStatus();
        set({
          feedback: {
            kind: "success",
            message: response.data.message,
            source: actionKey,
          },
        });
        return;
      }

      set({
        feedback: {
          kind: "error",
          message: response.data.message,
          source: actionKey,
        },
      });
    } catch (error) {
      const message = getApiErrorMessage(error);
      set({
        connectionTest: {
          success: false,
          balance_usdt: null,
          message,
        },
        feedback: {
          kind: "error",
          message,
          source: actionKey,
        },
      });
    } finally {
      set((state) => {
        const pendingActions = { ...state.pendingActions };
        delete pendingActions[actionKey];
        return { pendingActions };
      });
    }
  },

  connectSocket: () => {
    if (typeof window === "undefined") {
      return;
    }

    const reconnectTimer = get().socketReconnectTimer;
    if (reconnectTimer != null) {
      window.clearTimeout(reconnectTimer);
      set({ socketReconnectTimer: undefined });
    }

    const existingSocket = get().socket;
    if (
      existingSocket &&
      existingSocket.readyState !== WebSocket.CLOSING &&
      existingSocket.readyState !== WebSocket.CLOSED
    ) {
      return;
    }
    if (existingSocket) {
      set({ socket: undefined });
    }

    const socket = new WebSocket(getWebSocketUrl());
    set({ socket, socketManuallyClosed: false });

    socket.onopen = () => {
      if (get().socket !== socket) {
        return;
      }
      set({ socketReconnectAttempts: 0, socketReconnectTimer: undefined });
    };

    socket.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as { event?: string };
        if (typeof data.event !== "string") {
          return;
        }
        scheduleSocketEventRefresh(get, data.event);
      } catch {
        return;
      }
    };

    socket.onclose = () => {
      const isCurrentSocket = get().socket === socket;
      const isManualClose = get().socketManuallyClosed;
      if (!isCurrentSocket) {
        return;
      }

      set({ socket: undefined });
      if (isManualClose) {
        return;
      }

      const reconnectAttempts = get().socketReconnectAttempts + 1;
      const reconnectDelayMs = Math.min(15000, 1000 * 2 ** (reconnectAttempts - 1));
      const timer = window.setTimeout(() => {
        set({ socketReconnectTimer: undefined });
        get().connectSocket();
      }, reconnectDelayMs);

      set({
        socketReconnectAttempts: reconnectAttempts,
        socketReconnectTimer: timer,
      });
    };
  },

  disconnectSocket: () => {
    clearQueuedSocketRefreshTimer();
    queuedSocketEvents = new Set();

    const reconnectTimer = get().socketReconnectTimer;
    if (reconnectTimer != null) {
      window.clearTimeout(reconnectTimer);
    }

    const socket = get().socket;
    set({
      socket: undefined,
      socketReconnectTimer: undefined,
      socketReconnectAttempts: 0,
      socketManuallyClosed: true,
    });
    socket?.close();
  },
}));
