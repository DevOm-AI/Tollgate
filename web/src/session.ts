// The API address and admin key, kept for this browser tab only (sessionStorage), so
// closing the tab signs out.

export interface Session {
  baseUrl: string;
  adminKey: string;
}

const STORAGE_KEY = "tollgate.session";

export const DEFAULT_API_URL: string = import.meta.env.VITE_API_URL ?? "http://localhost:8001";

export function loadSession(): Session | null {
  try {
    const saved = sessionStorage.getItem(STORAGE_KEY);
    return saved ? (JSON.parse(saved) as Session) : null;
  } catch {
    return null;
  }
}

export function saveSession(session: Session | null): void {
  try {
    if (session) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session));
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Storage blocked (private mode): the session just won't survive a reload.
  }
}
