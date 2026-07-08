const BACKEND_ORIGIN = import.meta.env.VITE_BACKEND_BASE_URL || "https://fund-vista.fastapicloud.dev";

export const getApiBaseUrl = (path: string) => {
  return `${BACKEND_ORIGIN.replace(/\/$/, "")}${path}`;
};
