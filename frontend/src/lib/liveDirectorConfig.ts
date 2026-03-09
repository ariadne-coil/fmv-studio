const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);

function normalizeOrigin(value: string): string {
  return value.trim().replace(/\/+$/, "");
}

export function getLiveDirectorWsUrl(): string | null {
  const configured = normalizeOrigin(process.env.NEXT_PUBLIC_LIVE_DIRECTOR_WS_ORIGIN || "");
  if (configured) {
    return `${configured.replace(/^http/i, "ws")}/ws/live-director`;
  }

  if (typeof window !== "undefined" && LOCAL_HOSTS.has(window.location.hostname)) {
    return "ws://localhost:8001/ws/live-director";
  }

  return null;
}

export function getLiveDirectorRealtimeProjectId(): string | null {
  const value = (process.env.NEXT_PUBLIC_GCP_PROJECT_ID || "").trim();
  return value || null;
}

export function getLiveDirectorRealtimeLocation(): string {
  return (process.env.NEXT_PUBLIC_VERTEX_MEDIA_LOCATION || "us-central1").trim();
}

export function getLiveDirectorRealtimeModel(): string {
  return (process.env.NEXT_PUBLIC_LIVE_DIRECTOR_MODEL || "gemini-live-2.5-flash-native-audio").trim();
}

export function hasLiveDirectorRealtimeConfig(): boolean {
  return !!getLiveDirectorWsUrl() && !!getLiveDirectorRealtimeProjectId();
}
