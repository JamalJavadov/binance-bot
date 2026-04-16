export interface ActionFeedback {
  kind: "success" | "error";
  message: string;
  source?: string;
}

export interface ReadStatus {
  loaded: boolean;
  stale: boolean;
  error?: string;
}
