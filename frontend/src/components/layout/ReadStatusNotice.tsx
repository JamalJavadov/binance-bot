import type { ReadStatus } from "../../types/ui";

type ReadStatusNoticeProps = {
  status: ReadStatus;
  unavailableMessage: string;
  staleMessage?: string;
};

export function ReadStatusNotice({
  status,
  unavailableMessage,
  staleMessage,
}: ReadStatusNoticeProps) {
  if (!status.error) {
    return null;
  }

  const message = status.stale ? (staleMessage ?? unavailableMessage) : unavailableMessage;

  return (
    <div className={`notice-card ${status.stale ? "warning" : "error"}`} role={status.stale ? "status" : "alert"}>
      <p>{message}</p>
      <p className="muted">{status.error}</p>
    </div>
  );
}
