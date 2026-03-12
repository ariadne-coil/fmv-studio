const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
const DEFAULT_LIVE_DIRECTOR_MODEL = "gemini-live-2.5-flash-native-audio";
const GA_NATIVE_AUDIO_REGIONS = [
  "us-central1",
  "us-east1",
  "us-east4",
  "us-east5",
  "us-south1",
  "us-west1",
  "us-west4",
];

function normalizeOrigin(value: string): string {
  return value.trim().replace(/\/+$/, "");
}

function uniqueLocations(locations: string[]): string[] {
  return Array.from(new Set(locations.map((value) => value.trim()).filter(Boolean)));
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
  return (process.env.NEXT_PUBLIC_LIVE_DIRECTOR_MODEL || DEFAULT_LIVE_DIRECTOR_MODEL).trim();
}

export function getLiveDirectorRealtimeLocations(): string[] {
  const model = getLiveDirectorRealtimeModel();
  const primary = getLiveDirectorRealtimeLocation();
  const configuredFallbacks = uniqueLocations(
    (process.env.NEXT_PUBLIC_VERTEX_MEDIA_LOCATIONS || "")
      .split(","),
  );
  if (configuredFallbacks.length > 0) {
    return uniqueLocations([primary, ...configuredFallbacks]);
  }
  if (model === DEFAULT_LIVE_DIRECTOR_MODEL) {
    return uniqueLocations([primary, ...GA_NATIVE_AUDIO_REGIONS]);
  }
  return [primary];
}

export function hasLiveDirectorRealtimeConfig(): boolean {
  return !!getLiveDirectorWsUrl() && !!getLiveDirectorRealtimeProjectId();
}
