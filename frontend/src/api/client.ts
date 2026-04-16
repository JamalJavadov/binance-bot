import axios from "axios";

import { getApiBaseUrl } from "../lib/runtime";

export const api = axios.create({
  baseURL: getApiBaseUrl(),
  timeout: 15000,
});
