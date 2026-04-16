export type ScanStatus = "RUNNING" | "COMPLETE" | "FAILED";
export type ScanSymbolOutcome = "UNSUPPORTED" | "NO_SETUP" | "FILTERED_OUT" | "CANDIDATE" | "QUALIFIED" | "FAILED";
export type SignalDirection = "LONG" | "SHORT";
export type TriggerType = "AUTO_MODE";
export type SignalStatus =
  | "CANDIDATE"
  | "QUALIFIED"
  | "DISMISSED"
  | "APPROVED"
  | "EXPIRED"
  | "INVALIDATED"
  | "ORDER_FAILED";
export type OrderStatus =
  | "PENDING_APPROVAL"
  | "SUBMITTING"
  | "ORDER_PLACED"
  | "IN_POSITION"
  | "CLOSED_WIN"
  | "CLOSED_LOSS"
  | "CLOSED_BY_BOT"
  | "CLOSED_EXTERNALLY"
  | "CANCELLED_BY_BOT"
  | "CANCELLED_BY_USER";
export type OrderPreviewStatus = "affordable" | "resized_to_budget" | "too_small_for_exchange" | "not_affordable";

export interface CredentialsStatus {
  has_credentials: boolean;
  last_updated?: string | null;
  masked_api_key?: string | null;
}

export interface ConnectionTestResponse {
  success: boolean;
  balance_usdt?: number | null;
  message: string;
}

export interface ScanCycle {
  id: number;
  started_at: string;
  completed_at?: string | null;
  status: ScanStatus;
  symbols_scanned: number;
  candidates_found: number;
  signals_qualified: number;
  trigger_type: TriggerType;
  error_message?: string | null;
  progress_pct: number;
}

export interface ScanSymbolResult {
  symbol: string;
  direction?: SignalDirection | null;
  outcome: ScanSymbolOutcome;
  confirmation_score?: number | null;
  final_score?: number | null;
  score_breakdown: Record<string, number>;
  extra_context?: Record<string, unknown>;
  reason_text?: string | null;
  filter_reasons: string[];
  error_message?: string | null;
}

export interface WorkflowEvent {
  id: number;
  timestamp: string;
  event_type: string;
  level: string;
  symbol?: string | null;
  message?: string | null;
}

export interface ScanCycleDetail {
  cycle: ScanCycle;
  detail_available: boolean;
  results: ScanSymbolResult[];
  workflow: WorkflowEvent[];
}

export interface Signal {
  id: number;
  scan_cycle_id?: number | null;
  scan_trigger_type?: TriggerType | null;
  strategy_key?: string | null;
  strategy_label?: string | null;
  symbol: string;
  direction: SignalDirection;
  timeframe: string;
  entry_price: string;
  stop_loss: string;
  take_profit: string;
  rr_ratio: string;
  confirmation_score: number;
  final_score: number;
  score_breakdown: Record<string, number>;
  reason_text?: string | null;
  swing_origin?: string | null;
  swing_terminus?: string | null;
  fib_0786_level?: string | null;
  current_price_at_signal?: string | null;
  expires_at: string;
  status: SignalStatus;
  extra_context: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface SignalLiveReadiness {
  mark_price?: string | null;
  order_preview?: OrderPreview | null;
  can_open_now: boolean;
  failure_reason?: string | null;
}

export interface RecommendedSignal {
  rank: number;
  signal: Signal;
  live_readiness: SignalLiveReadiness;
}

export interface SignalRecommendationsResponse {
  latest_completed_scan_id?: number | null;
  latest_completed_scan_trigger_type?: TriggerType | null;
  latest_completed_scan_strategy_key?: string | null;
  latest_completed_scan_strategy_label?: string | null;
  refreshed_at: string;
  items: RecommendedSignal[];
}

export interface OrderPreview {
  status: OrderPreviewStatus;
  can_place: boolean;
  auto_resized: boolean;
  requested_quantity: string;
  final_quantity: string;
  max_affordable_quantity: string;
  mark_price_used: string;
  entry_notional: string;
  required_initial_margin: string;
  estimated_entry_fee: string;
  available_balance: string;
  reserve_balance: string;
  usable_balance: string;
  risk_budget_usdt: string;
  risk_usdt_at_stop: string;
  recommended_leverage: number;
  reason?: string | null;
}

export interface Order {
  id: number;
  signal_id?: number | null;
  symbol: string;
  direction: SignalDirection;
  leverage: number;
  margin_type: string;
  entry_price: string;
  stop_loss: string;
  take_profit: string;
  quantity: string;
  position_margin: string;
  notional_value: string;
  rr_ratio: string;
  entry_order_id?: string | null;
  tp_order_id?: string | null;
  partial_tp_enabled?: boolean | null;
  take_profit_1?: string | null;
  take_profit_2?: string | null;
  tp_quantity_1?: string | null;
  tp_quantity_2?: string | null;
  tp_order_1_id?: string | null;
  tp_order_2_id?: string | null;
  tp1_filled_at?: string | null;
  remaining_quantity?: string | null;
  sl_order_id?: string | null;
  status: OrderStatus;
  placed_at?: string | null;
  triggered_at?: string | null;
  closed_at?: string | null;
  cancelled_at?: string | null;
  cancel_reason?: string | null;
  expires_at: string;
  realized_pnl?: string | null;
  close_price?: string | null;
  close_type?: string | null;
  risk_budget_usdt: string;
  risk_usdt_at_stop: string;
  risk_pct_of_wallet: string;
  approved_by: string;
  created_at: string;
  updated_at: string;
}

export interface AutoModeStatus {
  enabled: boolean;
  paused: boolean;
  running: boolean;
  signal_schedule: string;
  kill_switch_active: boolean;
  kill_switch_reason?: string | null;
  active_order_count: number;
  active_risk_usdt: string;
  portfolio_risk_budget_usdt: string;
  per_slot_risk_budget_usdt: string;
  last_cycle_started_at?: string | null;
  last_cycle_completed_at?: string | null;
  next_cycle_at?: string | null;
}

export interface BalanceResponse {
  asset: string;
  balance: number;
  available_balance: number;
  usable_balance: number;
  reserve_balance: number;
}

export interface HealthStatus {
  backend_ok: boolean;
  db_ok: boolean;
  binance_reachable: boolean;
  server_time_offset_ms?: number | null;
}

export interface Position {
  symbol: string;
  position_side: string;
  direction: SignalDirection;
  position_amount: number;
  entry_price: number;
  mark_price: number;
  unrealized_pnl: number;
  leverage?: number | null;
  source_kind: "APP_LINKED" | "EXTERNAL" | string;
  linked_order_id?: number | null;
  first_seen_at?: string | null;
  last_seen_at?: string | null;
  closed_at?: string | null;
}

export interface PortfolioSummary {
  open_position_count: number;
  winning_position_count: number;
  losing_position_count: number;
  total_unrealized_pnl: number;
  last_synced_at?: string | null;
}

export interface SettingsResponse {
  settings: Record<string, string>;
}

export interface AuditEntry {
  id: number;
  timestamp: string;
  event_type: string;
  symbol?: string | null;
  message?: string | null;
  level: string;
  details: Record<string, unknown>;
}
