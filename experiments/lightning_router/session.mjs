export const KEY = /^sk-tr-v1-[A-Za-z0-9_-]{43}$/;
export const SESSION = "lightningrouter-usd-session-v1";

export function savedSession() {
  try {
    const value = JSON.parse(sessionStorage.getItem(SESSION));
    return value && KEY.test(value.key) && /^[a-f0-9]{32}$/.test(value.requestId) ? value : null;
  } catch { return null; }
}
