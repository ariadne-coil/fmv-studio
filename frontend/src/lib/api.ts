export type AgentStage = 'input' | 'lyria_prompting' | 'planning' | 'storyboarding' | 'filming' | 'production' | 'halted_for_review' | 'completed';

export interface MediaAsset {
    id: string;
    url: string;
    type: string;
    name: string;
    label?: string | null;
    mime_type?: string | null;
    text_content?: string | null;
    ai_context?: string | null;
    source?: string;
    purpose?: string | null;
}

export interface VideoClip {
    id: string;
    timeline_start: number;
    duration: number;
    storyboard_text: string;
    image_prompt?: string;
    image_url?: string;
    image_critiques: string[];
    image_approved: boolean | null;
    image_score?: number | null;
    image_reference_ready?: boolean;
    image_manual_override?: boolean;
    video_prompt?: string;
    video_url?: string;
    video_quality?: 'fast' | 'quality';
    video_critiques: string[];
    video_score?: number | null;
    video_approved: boolean | null;
}

export interface ProductionTimelineFragment {
    id: string;
    track_type?: "video" | "music";
    source_clip_id?: string | null;
    timeline_start: number;
    source_start: number;
    duration: number;
    audio_enabled?: boolean;
}

export interface ActivePipelineRun {
    run_id: string;
    stage: AgentStage;
    status: 'queued' | 'running';
    driver: string;
    started_at: string;
    updated_at: string;
}

export interface StageSummary {
    text: string;
    audio_url?: string;
    generated_at: string;
}

export interface DirectorTurn {
    id: string;
    role: "user" | "agent";
    text: string;
    audio_url?: string | null;
    stage: string;
    created_at: string;
    source?: string | null;
    applied_changes: string[];
}

export interface ProjectState {
    project_id: string;
    name: string;
    current_stage: AgentStage;
    screenplay: string;
    instructions: string;
    additional_lore: string;
    music_url?: string;
    music_duration_seconds?: number | null;
    image_provider?: string | null;
    video_provider?: string | null;
    music_provider?: string | null;
    music_workflow?: string;
    lyrics_prompt?: string;
    style_prompt?: string;
    music_min_duration_seconds?: number | null;
    music_max_duration_seconds?: number | null;
    music_start_seconds?: number | null;
    generated_music_provider?: string | null;
    generated_music_lyrics_prompt?: string | null;
    generated_music_style_prompt?: string | null;
    generated_music_min_duration_seconds?: number | null;
    generated_music_max_duration_seconds?: number | null;
    veo_quality: 'fast' | 'quality';
    assets: MediaAsset[];
    timeline: VideoClip[];
    production_timeline: ProductionTimelineFragment[];
    final_video_url?: string;
    last_error?: string;
    active_run?: ActivePipelineRun | null;
    stage_summaries: Record<string, StageSummary>;
    director_log: DirectorTurn[];
}

export interface ProjectSummary {
    project_id: string;
    name: string;
    current_stage: AgentStage;
    updated_at: string;
    final_video_url?: string;
}

export interface ProjectRunStatus {
    is_running: boolean;
    stage?: AgentStage | null;
    started_at?: string | null;
    status?: 'queued' | 'running' | null;
    driver?: string | null;
}

export interface LiveDirectorRequest {
    message: string;
    display_stage?: AgentStage;
    selected_clip_id?: string | null;
    selected_fragment_id?: string | null;
    selected_asset_id?: string | null;
    source?: "text" | "voice";
    speech_mode?: "standard" | "realtime";
}

export interface LiveDirectorResponse {
    project: ProjectState;
    reply_text: string;
    applied_changes: string[];
    target_clip_id?: string | null;
    target_fragment_id?: string | null;
    target_asset_id?: string | null;
    stage: AgentStage;
    navigation_action?: "stay" | "advance" | "rewind";
    target_stage?: AgentStage | null;
}

const API_URL = "/api";
const LOCAL_APP_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
const DIRECT_UPLOAD_MIN_BYTES = 8 * 1024 * 1024;

interface AssetUploadPlan {
    mode: "proxy" | "direct";
    upload_url?: string;
    upload_method?: string;
    upload_headers?: Record<string, string>;
    url?: string;
    name?: string;
    label?: string | null;
    asset_type?: string;
    mime_type?: string | null;
    text_content?: string | null;
    ai_context?: string | null;
}

export function shouldShowApiKeySettings(): boolean {
    if (typeof window === "undefined") return false;
    return LOCAL_APP_HOSTS.has(window.location.hostname);
}

function isNetworkError(error: unknown): boolean {
    return error instanceof TypeError && error.message === "Failed to fetch";
}

function toApiError(message: string, error: unknown): Error {
    if (isNetworkError(error)) {
        return new Error("Cannot reach the FMV Studio API. If you are running locally, make sure both the frontend and backend servers are started.");
    }
    if (error instanceof Error) {
        return error;
    }
    return new Error(message);
}

async function getApiErrorMessage(response: Response, fallbackMessage: string): Promise<string> {
    try {
        const data = await response.clone().json();
        if (typeof data?.detail === "string" && data.detail.trim()) {
            return data.detail;
        }
    } catch {}

    try {
        const text = await response.text();
        if (text.trim()) {
            return text.trim();
        }
    } catch {}

    return fallbackMessage;
}

export function toBackendAssetUrl(pathOrUrl?: string): string {
    if (!pathOrUrl) return "";
    if (pathOrUrl.startsWith("http://") || pathOrUrl.startsWith("https://")) {
        return pathOrUrl;
    }

    const normalized = pathOrUrl.replaceAll("\\", "/");
    const basePath = normalized.startsWith("/") ? normalized : `/${normalized}`;
    if (typeof window !== "undefined") {
        return new URL(basePath, window.location.origin).toString();
    }
    return basePath;
}

// ── API Key localStorage helpers ──────────────────────────────────────────────
export const SETTINGS_KEY = "fmv_gemini_api_key";

export function getStoredApiKey(): string {
    if (typeof window === "undefined") return "";
    return localStorage.getItem(SETTINGS_KEY) ?? "";
}

export function setStoredApiKey(key: string): void {
    if (typeof window === "undefined") return;
    if (key.trim()) {
        localStorage.setItem(SETTINGS_KEY, key.trim());
    } else {
        localStorage.removeItem(SETTINGS_KEY);
    }
}

// ── Models localStorage helpers ───────────────────────────────────────────────
export interface AppModels {
    orchestrator: string;
    critic: string;
    image: string;
    video: string;
    music: string;
}

export type MusicProviderId =
    | "google-lyria-realtime"
    | "external-import";

export interface MusicProviderOption {
    id: MusicProviderId;
    label: string;
    description: string;
    mode: "automatic" | "manual_import";
    usesLyrics: boolean;
    official: boolean;
    available: boolean;
    availabilityNote?: string;
}

export const DEFAULT_MUSIC_PROVIDER: MusicProviderId = "google-lyria-realtime";

export const MUSIC_PROVIDER_OPTIONS: MusicProviderOption[] = [
    {
        id: "google-lyria-realtime",
        label: "Google Lyria",
        description: "Official Google route. Uses Vertex AI in cloud deployments and the Live Music API locally. Instrumental-only.",
        mode: "automatic",
        usesLyrics: false,
        official: true,
        available: true,
    },
    {
        id: "external-import",
        label: "Manual Song Import",
        description: "Draft prompts in FMV Studio, then import a rendered song from another tool.",
        mode: "manual_import",
        usesLyrics: true,
        official: false,
        available: true,
    },
];

const LEGACY_MUSIC_PROVIDER_ALIASES: Record<string, MusicProviderId> = {
    "lyria-realtime-exp": "google-lyria-realtime",
    "external-lyria-3": "external-import",
    "lyria-3": "external-import",
};

export function normalizeMusicProviderId(value?: string | null): MusicProviderId {
    const normalized = (value ?? "").trim();
    if (!normalized) return DEFAULT_MUSIC_PROVIDER;

    const matchedOption = MUSIC_PROVIDER_OPTIONS.find((option) => option.id === normalized);
    if (matchedOption) return matchedOption.id;

    return LEGACY_MUSIC_PROVIDER_ALIASES[normalized] ?? DEFAULT_MUSIC_PROVIDER;
}

export function getMusicProviderOption(value?: string | null): MusicProviderOption {
    const normalized = normalizeMusicProviderId(value);
    return MUSIC_PROVIDER_OPTIONS.find((option) => option.id === normalized) ?? MUSIC_PROVIDER_OPTIONS[0];
}

export function isManualImportMusicProvider(value?: string | null): boolean {
    return getMusicProviderOption(value).mode === "manual_import";
}

export interface AppPreferences {
    stageVoiceBriefsEnabled: boolean;
    imageResolution: ImageResolution;
    videoResolution: VideoResolution;
}

export type ImageResolution = "1K" | "2K" | "4K";
export type VideoResolution = "720p" | "1080p" | "4k";

export interface ResolutionOption<T extends string> {
    value: T;
    label: string;
    description: string;
}

export const IMAGE_RESOLUTION_OPTIONS: ResolutionOption<ImageResolution>[] = [
    { value: "1K", label: "1K", description: "Fastest and lowest-cost storyboard frames." },
    { value: "2K", label: "2K", description: "Sharper storyboard frames with a moderate cost increase." },
    { value: "4K", label: "4K", description: "Maximum storyboard detail for stronger source frames." },
];

export const VIDEO_RESOLUTION_OPTIONS: ResolutionOption<VideoResolution>[] = [
    { value: "720p", label: "720p", description: "Faster Veo generation with lower output detail." },
    { value: "1080p", label: "1080p", description: "High-quality Veo renders with a balanced cost/performance tradeoff." },
    { value: "4k", label: "4K", description: "Maximum Veo output detail at the highest cost." },
];

export function normalizeImageResolution(value?: string | null): ImageResolution {
    const normalized = (value ?? "").trim().toUpperCase();
    if (normalized === "1K" || normalized === "2K" || normalized === "4K") return normalized;
    return "4K";
}

export function normalizeVideoResolution(value?: string | null): VideoResolution {
    const normalized = (value ?? "").trim().toLowerCase();
    if (normalized === "720p" || normalized === "1080p" || normalized === "4k") return normalized;
    return "1080p";
}

export const DEFAULT_MODELS: AppModels = {
    orchestrator: "gemini-3-pro-preview",
    critic: "gemini-3-flash-preview",
    image: "gemini-2.5-flash-image",
    video: "veo-3.1-fast-generate-001",
    music: DEFAULT_MUSIC_PROVIDER,
};

export const SETTINGS_MODELS_KEY = "fmv_models";
export const SETTINGS_PREFERENCES_KEY = "fmv_preferences";

export function getStoredModels(): AppModels {
    if (typeof window === "undefined") return DEFAULT_MODELS;
    try {
        const stored = localStorage.getItem(SETTINGS_MODELS_KEY);
        if (stored) {
            const parsed = JSON.parse(stored);

            // Legacy Cache Migration
            if (parsed.text && !parsed.orchestrator) parsed.orchestrator = parsed.text;
            if (parsed.orchestrator === "gemini-3.1-pro-preview") parsed.orchestrator = "gemini-3-pro-preview";
            if (!parsed.critic) parsed.critic = DEFAULT_MODELS.critic;
            if (parsed.video === "veo-3.1-fast") parsed.video = "veo-3.1-fast-generate-001";
            if (parsed.video === "veo-3.1-quality") parsed.video = "veo-3.1-generate-001";
            if (parsed.video === "veo-3.1-fast-generate-preview") parsed.video = "veo-3.1-fast-generate-001";
            if (parsed.video === "veo-3.1-generate-preview") parsed.video = "veo-3.1-generate-001";
            if (parsed.image === "nanobanana-2") parsed.image = "gemini-2.5-flash-image";
            if (parsed.image === "nanobanana-pro") parsed.image = "gemini-3-pro-image-preview";
            if (parsed.image === "gemini-3.1-flash-image-preview") parsed.image = "gemini-2.5-flash-image";
            parsed.music = normalizeMusicProviderId(parsed.music);

            return { ...DEFAULT_MODELS, ...parsed };
        }
    } catch (e) {
        console.error("Failed to parse stored models", e);
    }
    return DEFAULT_MODELS;
}

export function setStoredModels(models: AppModels): void {
    if (typeof window === "undefined") return;
    localStorage.setItem(SETTINGS_MODELS_KEY, JSON.stringify(models));
}

export const DEFAULT_PREFERENCES: AppPreferences = {
    stageVoiceBriefsEnabled: true,
    imageResolution: "4K",
    videoResolution: "1080p",
};

export function getStoredPreferences(): AppPreferences {
    if (typeof window === "undefined") return DEFAULT_PREFERENCES;
    try {
        const stored = localStorage.getItem(SETTINGS_PREFERENCES_KEY);
        if (stored) {
            const parsed = JSON.parse(stored);
            return {
                ...DEFAULT_PREFERENCES,
                ...parsed,
                imageResolution: normalizeImageResolution(parsed?.imageResolution),
                videoResolution: normalizeVideoResolution(parsed?.videoResolution),
            };
        }
    } catch (e) {
        console.error("Failed to parse stored preferences", e);
    }
    return DEFAULT_PREFERENCES;
}

export function setStoredPreferences(preferences: AppPreferences): void {
    if (typeof window === "undefined") return;
    localStorage.setItem(SETTINGS_PREFERENCES_KEY, JSON.stringify(preferences));
}

// ── API client ────────────────────────────────────────────────────────────────
export const api = {
    async createProject(id: string, name: string): Promise<ProjectState> {
        try {
            const res = await fetch(`${API_URL}/projects`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ project_id: id, name: name })
            });
            if (!res.ok) throw new Error("Failed to create project");
            return res.json();
        } catch (error) {
            throw toApiError("Failed to create project", error);
        }
    },

    async listProjects(): Promise<ProjectSummary[]> {
        try {
            const res = await fetch(`${API_URL}/projects`);
            if (!res.ok) throw new Error("Failed to list projects");
            return res.json();
        } catch (error) {
            throw toApiError("Failed to list projects", error);
        }
    },

    async getProject(id: string): Promise<ProjectState> {
        try {
            const res = await fetch(`${API_URL}/projects/${id}`);
            if (!res.ok) throw new Error("Failed to fetch project");
            return res.json();
        } catch (error) {
            throw toApiError("Failed to fetch project", error);
        }
    },

    async deleteProject(id: string): Promise<void> {
        try {
            const res = await fetch(`${API_URL}/projects/${id}`, {
                method: "DELETE",
            });
            if (!res.ok) {
                throw new Error(await getApiErrorMessage(res, "Failed to delete project"));
            }
        } catch (error) {
            throw toApiError("Failed to delete project", error);
        }
    },

    async updateProject(id: string, state: ProjectState): Promise<ProjectState> {
        try {
            const res = await fetch(`${API_URL}/projects/${id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(state)
            });
            if (!res.ok) throw new Error("Failed to update project");
            return res.json();
        } catch (error) {
            throw toApiError("Failed to update project", error);
        }
    },

    async updateAssetLabel(id: string, assetId: string, label: string | null): Promise<ProjectState> {
        const key = getStoredApiKey();
        try {
            const res = await fetch(`${API_URL}/projects/${id}/assets/${assetId}/label`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    ...(key ? { "X-API-Key": key } : {}),
                },
                body: JSON.stringify({ label }),
            });
            if (!res.ok) {
                throw new Error(await getApiErrorMessage(res, "Failed to save asset label"));
            }
            return res.json();
        } catch (error) {
            throw toApiError("Failed to save asset label", error);
        }
    },

    async uploadAsset(projectId: string, file: File): Promise<{
        url: string;
        name: string;
        label?: string;
        asset_type?: string;
        mime_type?: string | null;
        text_content?: string | null;
        ai_context?: string | null;
    }> {
        const key = getStoredApiKey();
        try {
            if (file.size >= DIRECT_UPLOAD_MIN_BYTES) {
                const planResponse = await fetch(`${API_URL}/projects/${projectId}/upload-plan`, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        ...(key ? { "X-API-Key": key } : {}),
                    },
                    body: JSON.stringify({
                        filename: file.name,
                        content_type: file.type || null,
                        size: file.size,
                    }),
                });
                if (!planResponse.ok) {
                    throw new Error(await getApiErrorMessage(planResponse, "Failed to prepare asset upload"));
                }

                const plan = await planResponse.json() as AssetUploadPlan;
                if (plan.mode === "direct" && plan.upload_url && plan.url) {
                    const uploadHeaders = new Headers(plan.upload_headers ?? {});
                    if (!uploadHeaders.has("Content-Type") && file.type) {
                        uploadHeaders.set("Content-Type", file.type);
                    }
                    const directUploadResponse = await fetch(plan.upload_url, {
                        method: plan.upload_method || "PUT",
                        headers: uploadHeaders,
                        body: file,
                    });
                    if (!directUploadResponse.ok) {
                        const failureMessage = directUploadResponse.status === 413
                            ? "This file is too large for direct upload."
                            : "Failed to upload asset bytes.";
                        throw new Error(await getApiErrorMessage(directUploadResponse, failureMessage));
                    }

                    const finalizeResponse = await fetch(`${API_URL}/projects/${projectId}/upload-complete`, {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json",
                            ...(key ? { "X-API-Key": key } : {}),
                        },
                        body: JSON.stringify({
                            url: plan.url,
                            filename: plan.name || file.name,
                            content_type: (plan.mime_type ?? file.type) || null,
                        }),
                    });
                    if (!finalizeResponse.ok) {
                        throw new Error(await getApiErrorMessage(finalizeResponse, "Failed to analyze uploaded asset"));
                    }
                    const finalized = await finalizeResponse.json() as AssetUploadPlan;

                    return {
                        url: finalized.url || plan.url,
                        name: finalized.name || plan.name || file.name,
                        label: finalized.label ?? plan.label ?? undefined,
                        asset_type: finalized.asset_type || plan.asset_type,
                        mime_type: finalized.mime_type ?? plan.mime_type ?? file.type,
                        text_content: finalized.text_content ?? undefined,
                        ai_context: finalized.ai_context ?? undefined,
                    };
                }
            }

            const formData = new FormData();
            formData.append("file", file);
            const res = await fetch(`${API_URL}/projects/${projectId}/upload`, {
                method: "POST",
                headers: key ? { "X-API-Key": key } : undefined,
                body: formData,
            });
            if (!res.ok) {
                const fallbackMessage = res.status === 413
                    ? "This file is too large for the standard upload path. Try again and the app will use direct cloud upload for large audio/video/image files."
                    : "Upload failed";
                throw new Error(await getApiErrorMessage(res, fallbackMessage));
            }
            return res.json();
        } catch (error) {
            throw toApiError("Upload failed", error);
        }
    },

    async regenerateMusic(id: string): Promise<ProjectState> {
        const key = getStoredApiKey();
        const models = getStoredModels();
        const headers: Record<string, string> = {
            "X-Music-Model": models.music,
        };
        if (key) headers["X-API-Key"] = key;

        try {
            const res = await fetch(`${API_URL}/projects/${id}/regenerate-music`, {
                method: "POST",
                headers,
            });
            if (!res.ok) {
                throw new Error(await getApiErrorMessage(res, "Failed to regenerate the song"));
            }
            return res.json();
        } catch (error) {
            throw toApiError("Failed to regenerate the song", error);
        }
    },

    async runPipeline(id: string, signal?: AbortSignal): Promise<ProjectState> {
        const key = getStoredApiKey();
        const models = getStoredModels();
        const preferences = getStoredPreferences();
        const headers: Record<string, string> = {
            "X-Orchestrator-Model": models.orchestrator,
            "X-Critic-Model": models.critic,
            "X-Text-Model": models.orchestrator,
            "X-Image-Model": models.image,
            "X-Image-Resolution": preferences.imageResolution,
            "X-Video-Model": models.video,
            "X-Video-Resolution": preferences.videoResolution,
            "X-Music-Model": models.music,
            "X-Stage-Voice-Briefs-Enabled": String(preferences.stageVoiceBriefsEnabled),
        };
        if (key) headers["X-API-Key"] = key;

        try {
            const res = await fetch(`${API_URL}/projects/${id}/run`, {
                method: 'POST',
                headers,
                signal,
            });
            if (!res.ok) throw new Error("Failed to run pipeline step");
            return res.json();
        } catch (error) {
            throw toApiError("Failed to run pipeline step", error);
        }
    },

    async runPipelineAsync(id: string): Promise<ProjectState> {
        const key = getStoredApiKey();
        const models = getStoredModels();
        const preferences = getStoredPreferences();
        const headers: Record<string, string> = {
            "X-Orchestrator-Model": models.orchestrator,
            "X-Critic-Model": models.critic,
            "X-Text-Model": models.orchestrator,
            "X-Image-Model": models.image,
            "X-Image-Resolution": preferences.imageResolution,
            "X-Video-Model": models.video,
            "X-Video-Resolution": preferences.videoResolution,
            "X-Music-Model": models.music,
            "X-Stage-Voice-Briefs-Enabled": String(preferences.stageVoiceBriefsEnabled),
        };
        if (key) headers["X-API-Key"] = key;

        try {
            const res = await fetch(`${API_URL}/projects/${id}/run-async`, {
                method: 'POST',
                headers,
            });
            if (!res.ok) throw new Error("Failed to start background pipeline run");
            return res.json();
        } catch (error) {
            throw toApiError("Failed to start background pipeline run", error);
        }
    },

    async getRunStatus(id: string): Promise<ProjectRunStatus> {
        try {
            const res = await fetch(`${API_URL}/projects/${id}/run-status`);
            if (!res.ok) throw new Error("Failed to fetch pipeline run status");
            return res.json();
        } catch (error) {
            throw toApiError("Failed to fetch pipeline run status", error);
        }
    },

    async revert(id: string, targetStage: string): Promise<ProjectState> {
        try {
            const res = await fetch(`${API_URL}/projects/${id}/revert`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ target_stage: targetStage }),
            });
            if (!res.ok) throw new Error("Failed to revert pipeline");
            return res.json();
        } catch (error) {
            throw toApiError("Failed to revert pipeline", error);
        }
    },

    async liveDirector(id: string, request: LiveDirectorRequest): Promise<LiveDirectorResponse> {
        const key = getStoredApiKey();
        const models = getStoredModels();
        const preferences = getStoredPreferences();
        const headers: Record<string, string> = {
            "Content-Type": "application/json",
            "X-Orchestrator-Model": models.orchestrator,
            "X-Critic-Model": models.critic,
            "X-Text-Model": models.orchestrator,
            "X-Image-Model": models.image,
            "X-Image-Resolution": preferences.imageResolution,
            "X-Video-Model": models.video,
            "X-Video-Resolution": preferences.videoResolution,
            "X-Music-Model": models.music,
            "X-Stage-Voice-Briefs-Enabled": String(preferences.stageVoiceBriefsEnabled),
        };
        if (key) headers["X-API-Key"] = key;

        try {
            const res = await fetch(`${API_URL}/projects/${id}/live-director`, {
                method: "POST",
                headers,
                body: JSON.stringify(request),
            });
            if (!res.ok) {
                throw new Error(await getApiErrorMessage(res, "Live Director Mode failed"));
            }
            return res.json();
        } catch (error) {
            throw toApiError("Live Director Mode failed", error);
        }
    },

    async updateStoryboardClipApproval(id: string, clipId: string, approved: boolean): Promise<ProjectState> {
        const key = getStoredApiKey();
        try {
            const res = await fetch(`${API_URL}/projects/${id}/clips/${clipId}/image-approval`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    ...(key ? { "X-API-Key": key } : {}),
                },
                body: JSON.stringify({ approved }),
            });
            if (!res.ok) {
                throw new Error(await getApiErrorMessage(res, "Failed to update storyboard approval"));
            }
            return res.json();
        } catch (error) {
            throw toApiError("Failed to update storyboard approval", error);
        }
    },

    async updateStoryboardClipText(id: string, clipId: string, storyboardText: string): Promise<ProjectState> {
        const key = getStoredApiKey();
        try {
            const res = await fetch(`${API_URL}/projects/${id}/storyboard-clips/${clipId}/text`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    ...(key ? { "X-API-Key": key } : {}),
                },
                body: JSON.stringify({ storyboard_text: storyboardText }),
            });
            if (!res.ok) {
                throw new Error(await getApiErrorMessage(res, "Failed to save storyboard prompt"));
            }
            return res.json();
        } catch (error) {
            throw toApiError("Failed to save storyboard prompt", error);
        }
    },

    async updateFilmingClipApproval(id: string, clipId: string, approved: boolean): Promise<ProjectState> {
        const key = getStoredApiKey();
        try {
            const res = await fetch(`${API_URL}/projects/${id}/clips/${clipId}/video-approval`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    ...(key ? { "X-API-Key": key } : {}),
                },
                body: JSON.stringify({ approved }),
            });
            if (!res.ok) {
                throw new Error(await getApiErrorMessage(res, "Failed to update video approval"));
            }
            return res.json();
        } catch (error) {
            throw toApiError("Failed to update video approval", error);
        }
    },

    async regenerateStoryboardClip(id: string, clipId: string): Promise<ProjectState> {
        const key = getStoredApiKey();
        const models = getStoredModels();
        const preferences = getStoredPreferences();
        const headers: Record<string, string> = {
            "X-Orchestrator-Model": models.orchestrator,
            "X-Critic-Model": models.critic,
            "X-Text-Model": models.orchestrator,
            "X-Image-Model": models.image,
            "X-Image-Resolution": preferences.imageResolution,
        };
        if (key) headers["X-API-Key"] = key;

        try {
            const res = await fetch(`${API_URL}/projects/${id}/storyboard-clips/${clipId}/regenerate`, {
                method: "POST",
                headers,
            });
            if (!res.ok) {
                throw new Error(await getApiErrorMessage(res, "Failed to regenerate storyboard frame"));
            }
            return res.json();
        } catch (error) {
            throw toApiError("Failed to regenerate storyboard frame", error);
        }
    },

    async uploadStoryboardFrame(
        id: string,
        clipId: string,
        payload: { url: string; name: string }
    ): Promise<ProjectState> {
        const key = getStoredApiKey();
        try {
            const res = await fetch(`${API_URL}/projects/${id}/storyboard-clips/${clipId}/upload-frame`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    ...(key ? { "X-API-Key": key } : {}),
                },
                body: JSON.stringify(payload),
            });
            if (!res.ok) {
                throw new Error(await getApiErrorMessage(res, "Failed to replace storyboard frame"));
            }
            return res.json();
        } catch (error) {
            throw toApiError("Failed to replace storyboard frame", error);
        }
    }
}
