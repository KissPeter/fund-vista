const API_PORT = "8001";

export const getApiBaseUrl = (path: string) => {
  if (typeof window === "undefined") {
    return `http://localhost:${API_PORT}${path}`;
  }

  const url = new URL(window.location.origin);
  url.port = API_PORT;
  return `${url.origin}${path}`;
};
