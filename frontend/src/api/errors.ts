import axios from "axios";

type ErrorPayload = {
  detail?: string;
  message?: string;
};

const GENERIC_API_ERROR = "Request failed. Check the backend connection and try again.";

export function getApiErrorMessage(error: unknown): string {
  if (axios.isAxiosError<ErrorPayload>(error)) {
    const detail = error.response?.data?.detail;
    const message = error.response?.data?.message;

    if (
      typeof detail === "string" &&
      detail.trim().length > 0 &&
      typeof message === "string" &&
      message.trim().length > 0
    ) {
      return detail.trim() === message.trim() ? detail : `${detail} ${message}`;
    }

    if (typeof detail === "string" && detail.trim().length > 0) {
      return detail;
    }

    if (typeof message === "string" && message.trim().length > 0) {
      return message;
    }

    if (typeof error.message === "string" && error.message.trim().length > 0) {
      return error.message;
    }
  }

  if (error instanceof Error && error.message.trim().length > 0) {
    return error.message;
  }

  return GENERIC_API_ERROR;
}
