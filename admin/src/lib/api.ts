export class Unauthorized extends Error {}

let unauthorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null) {
  unauthorizedHandler = handler;
}

export async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.headers || {}),
    },
  });
  if (response.status === 401) {
    if (!url.includes("/api/login")) unauthorizedHandler?.();
    throw new Unauthorized();
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.error || `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export async function requestBlob(url: string, init?: RequestInit): Promise<Blob> {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...init,
    headers: {
      Accept: "audio/mpeg",
      ...(init?.headers || {}),
    },
  });
  if (response.status === 401) {
    if (!url.includes("/api/login")) unauthorizedHandler?.();
    throw new Unauthorized();
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.error || `HTTP ${response.status}`);
  }
  return response.blob();
}
