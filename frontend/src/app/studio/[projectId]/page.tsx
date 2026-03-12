"use client";

import React, { useState, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { GlassCard } from "@/components/ui/GlassCard";
import LiveDirectorPanel from "@/components/ui/LiveDirectorPanel";
import ProductionTimelineEditor from "@/components/ui/ProductionTimelineEditor";
import SettingsModal from "@/components/ui/SettingsModal";
import ShotListEditor from "@/components/ui/ShotListEditor";
import StageTimelineOverview from "@/components/ui/StageTimelineOverview";
import AssetViewerWorkspace from "@/components/ui/AssetViewerWorkspace";
import { Loader2, Music, ImageIcon, Play, Pause, SkipBack, SkipForward, Wand2, X, AlertCircle, Save, Video, Settings, ListPlus, Maximize2, GripVertical, Home, FileText, Trash2, Plus, RefreshCw } from 'lucide-react';
import { api, getMusicProviderOption, getStoredApiKey, getStoredModels, getStoredPreferences, isManualImportMusicProvider, LiveDirectorResponse, normalizeMusicProviderId, ProductionTimelineFragment, ProjectRunStatus, ProjectState, toBackendAssetUrl, VideoClip } from "@/lib/api";

const MIN_PRODUCTION_FRAGMENT_DURATION = 0.25;
const MIN_MUSIC_FRAGMENT_DURATION = 0.25;
const DEFAULT_MUSIC_MIN_DURATION_SECONDS = 90;
const DEFAULT_MUSIC_MAX_DURATION_SECONDS = 240;
const DEFAULT_MUSIC_START_SECONDS = 0;
const STORYBOARD_VALID_DURATIONS = [4, 6, 8] as const;
const RANGE_DOWNLOAD_CHUNK_SIZE = 8 * 1024 * 1024;
let storyboardClipIdCounter = Date.now();

function quantizeStoryboardDuration(duration: number): 4 | 6 | 8 {
    const value = Number.isFinite(duration) ? duration : 6;
    return STORYBOARD_VALID_DURATIONS.reduce((best, candidate) => {
        const bestDelta = Math.abs(best - value);
        const candidateDelta = Math.abs(candidate - value);
        if (candidateDelta < bestDelta) return candidate;
        if (candidateDelta === bestDelta && candidate > best) return candidate;
        return best;
    }, STORYBOARD_VALID_DURATIONS[1]);
}

function recalcStoryboardTimeline(clips: VideoClip[]): VideoClip[] {
    let currentTime = 0;
    return clips.map((clip) => {
        const normalizedDuration = quantizeStoryboardDuration(clip.duration);
        const updated = {
            ...clip,
            duration: normalizedDuration,
            timeline_start: currentTime,
        };
        currentTime += normalizedDuration;
        return updated;
    });
}

function createStoryboardClip(): VideoClip {
    return {
        id: `clip_custom_${storyboardClipIdCounter++}`,
        timeline_start: 0,
        duration: 6,
        storyboard_text: "",
        image_critiques: [],
        image_approved: false,
        video_critiques: [],
        video_approved: false,
    };
}

function inferReviewStageForState(state: ProjectState | null | undefined): string {
    if (!state) return 'input';
    if (state.current_stage !== 'halted_for_review') {
        return state.current_stage;
    }
    if (state.final_video_url) {
        return 'completed';
    }
    if (state.production_timeline.some((fragment) => (fragment.track_type ?? "video") !== "music")) {
        return 'production';
    }
    if (state.timeline.some((clip) => !!clip.video_url || !!clip.video_prompt || clip.video_critiques.length > 0)) {
        return 'filming';
    }
    if (state.timeline.some((clip) => !!clip.image_url || !!clip.image_prompt || clip.image_critiques.length > 0)) {
        return 'storyboarding';
    }
    if (state.timeline.length > 0) {
        return 'planning';
    }
    if (state.music_workflow !== 'uploaded_track' && !state.music_url && ((state.lyrics_prompt ?? '').trim() || (state.style_prompt ?? '').trim())) {
        return 'lyria_prompting';
    }
    return 'input';
}

function getTimelineClipAtTime(clips: VideoClip[], seconds: number): VideoClip | null {
    if (!clips.length) return null;
    const orderedClips = [...clips].sort((left, right) => left.timeline_start - right.timeline_start);
    const clampedSeconds = Math.max(0, seconds);
    for (const clip of orderedClips) {
        const clipEnd = clip.timeline_start + clip.duration;
        if (clampedSeconds >= clip.timeline_start && clampedSeconds < clipEnd) {
            return clip;
        }
    }
    return orderedClips[orderedClips.length - 1] ?? null;
}

type FilmingEvaluatorSummary = {
    tone: "success" | "warning" | "error";
    text: string;
};

function getFilmingEvaluatorSummary(clip: VideoClip): FilmingEvaluatorSummary | null {
    const candidate = [...clip.video_critiques]
        .reverse()
        .find((entry) => {
            const trimmed = entry.trim();
            return (
                !!trimmed
                && !trimmed.startsWith("Ingredients used:")
                && !trimmed.startsWith("Manual video clip uploaded:")
            );
        });

    if (!candidate) return null;

    const normalized = candidate.trim()
        .replace(/^Score:\s*\d+\/10\s*[—-]\s*/u, "")
        .replace(/^All 3 video critics cleared this pass without a unanimous blocking concern\.\s*/u, "No blocking issue found. ")
        .replace(/^The 3 video critics did not reach unanimous agreement on any blocking issue\.\s*/u, "No unanimous blocking issue found. ")
        .replace(/^All 3 video critics independently flagged the same blocking concern\(s\):\s*/u, "Blocking issue flagged: ")
        .replace(/^Automated video review unavailable:\s*/iu, "Evaluator unavailable: ")
        .replace(/^.* generation failed:\s*/u, "Generation failed: ");

    if (/^Generation failed:/i.test(normalized)) {
        return {
            tone: "error",
            text: normalized,
        };
    }

    if (clip.video_score == null) {
        return {
            tone: "warning",
            text: normalized,
        };
    }

    if (clip.video_score >= 8) {
        return {
            tone: "success",
            text: normalized,
        };
    }

    if (clip.video_score >= 6) {
        return {
            tone: "warning",
            text: normalized,
        };
    }

    return {
        tone: "error",
        text: normalized,
    };
}

function normalizeMusicStartSeconds(value?: number | null): number {
    if (!Number.isFinite(value)) return DEFAULT_MUSIC_START_SECONDS;
    return Math.max(0, Number(Number(value).toFixed(3)));
}

function buildDefaultMusicProductionFragment(
    programDuration: number,
    musicDuration?: number | null,
    musicStartSeconds?: number | null,
): ProductionTimelineFragment | null {
    if (programDuration <= 0) {
        return null;
    }

    const effectiveMusicDuration = Number.isFinite(musicDuration) && (musicDuration ?? 0) > 0
        ? Math.max(MIN_MUSIC_FRAGMENT_DURATION, Math.min(programDuration, Number(musicDuration)))
        : null;
    const maxStart = Math.max(
        0,
        programDuration - (effectiveMusicDuration ?? MIN_MUSIC_FRAGMENT_DURATION),
    );
    const timelineStart = Math.min(normalizeMusicStartSeconds(musicStartSeconds), Number(maxStart.toFixed(3)));
    const duration = effectiveMusicDuration ?? Math.max(
        MIN_MUSIC_FRAGMENT_DURATION,
        Number((programDuration - timelineStart).toFixed(3)),
    );

    return {
        id: "music_frag_0",
        track_type: "music",
        source_clip_id: null,
        timeline_start: Number(timelineStart.toFixed(3)),
        source_start: 0,
        duration: Number(duration.toFixed(3)),
        audio_enabled: true,
    };
}

function buildDefaultProductionTimeline(
    clips: VideoClip[],
    options?: {
        includeMusic?: boolean;
        musicDuration?: number | null;
        musicStartSeconds?: number | null;
    }
): ProductionTimelineFragment[] {
    let currentTime = 0;
    const videoFragments = [...clips]
        .sort((left, right) => left.timeline_start - right.timeline_start)
        .map((clip) => {
            const fragment = {
                id: `${clip.id}_frag_0`,
                track_type: "video" as const,
                source_clip_id: clip.id,
                timeline_start: currentTime,
                source_start: 0,
                duration: clip.duration,
                audio_enabled: true,
            };
            currentTime += clip.duration;
            return fragment;
        });

    if (!options?.includeMusic || currentTime <= 0) {
        return videoFragments;
    }

    const defaultMusicFragment = buildDefaultMusicProductionFragment(
        currentTime,
        options.musicDuration,
        options.musicStartSeconds,
    );
    if (!defaultMusicFragment) {
        return videoFragments;
    }

    return [
        ...videoFragments,
        defaultMusicFragment,
    ];
}

function shouldSyncWholeClipProductionFragments(
    fragments: ProductionTimelineFragment[],
    clips: VideoClip[],
): boolean {
    const videoFragments = fragments.filter((fragment) => (fragment.track_type ?? "video") !== "music");
    if (!videoFragments.length || videoFragments.length !== clips.length) {
        return false;
    }

    const clipIds = new Set(clips.map((clip) => clip.id));
    const seenClipIds = new Set<string>();
    for (const fragment of videoFragments) {
        const sourceClipId = fragment.source_clip_id ?? "";
        if (!sourceClipId || !clipIds.has(sourceClipId) || seenClipIds.has(sourceClipId)) {
            return false;
        }
        if (Math.abs(fragment.source_start) > 0.001) {
            return false;
        }
        seenClipIds.add(sourceClipId);
    }

    return seenClipIds.size === clips.length;
}

function normalizeProductionTimeline(
    fragments: ProductionTimelineFragment[],
    clips: VideoClip[],
    musicDuration?: number | null,
): ProductionTimelineFragment[] {
    const clipDurationLookup = new Map(
        clips.map((clip) => [clip.id, Math.max(MIN_PRODUCTION_FRAGMENT_DURATION, Number(clip.duration.toFixed(3)))])
    );
    const syncWholeClipDurations = shouldSyncWholeClipProductionFragments(fragments, clips);
    let currentVideoTime = 0;
    const normalizedVideo = fragments
        .filter((fragment) => (fragment.track_type ?? "video") !== "music" && fragment.duration > 0)
        .map((fragment) => {
            const syncedDuration = syncWholeClipDurations && fragment.source_clip_id
                ? clipDurationLookup.get(fragment.source_clip_id) ?? null
                : null;
            const normalized = {
                ...fragment,
                track_type: "video" as const,
                source_start: syncWholeClipDurations
                    ? 0
                    : Math.max(0, Number(fragment.source_start.toFixed(3))),
                duration: Math.max(
                    MIN_PRODUCTION_FRAGMENT_DURATION,
                    Number((syncedDuration ?? fragment.duration).toFixed(3)),
                ),
                timeline_start: Number(currentVideoTime.toFixed(3)),
                audio_enabled: fragment.audio_enabled ?? true,
            };
            currentVideoTime += normalized.duration;
            return normalized;
        });

    const totalVideoDuration = normalizedVideo.reduce((sum, fragment) => sum + fragment.duration, 0);

    const normalizedMusic: ProductionTimelineFragment[] = [];
    let previousMusicEnd = 0;
    for (const fragment of fragments
        .filter((item) => (item.track_type ?? "video") === "music" && item.duration > 0)
        .sort((left, right) => left.timeline_start - right.timeline_start)) {
        let timelineStart = Math.max(0, Number(fragment.timeline_start.toFixed(3)));

        let sourceStart = Math.max(0, Number(fragment.source_start.toFixed(3)));
        if (Number.isFinite(musicDuration) && (musicDuration ?? 0) > 0) {
            sourceStart = Math.min(sourceStart, Math.max(0, (musicDuration ?? 0) - MIN_MUSIC_FRAGMENT_DURATION));
        }

        let duration = Math.max(MIN_MUSIC_FRAGMENT_DURATION, Number(fragment.duration.toFixed(3)));
        if (Number.isFinite(musicDuration) && (musicDuration ?? 0) > 0) {
            duration = Math.min(duration, Math.max(MIN_MUSIC_FRAGMENT_DURATION, (musicDuration ?? 0) - sourceStart));
        }
        if (duration <= 0) continue;

        if (totalVideoDuration > 0) {
            timelineStart = Math.min(timelineStart, Math.max(0, totalVideoDuration - duration));
        }
        timelineStart = Math.max(timelineStart, Number(previousMusicEnd.toFixed(3)));

        const normalized = {
            ...fragment,
            track_type: "music" as const,
            source_clip_id: null,
            timeline_start: Number(timelineStart.toFixed(3)),
            source_start: Number(sourceStart.toFixed(3)),
            duration: Number(duration.toFixed(3)),
            audio_enabled: true,
        };
        normalizedMusic.push(normalized);
        previousMusicEnd = normalized.timeline_start + normalized.duration;
    }

    return [...normalizedVideo, ...normalizedMusic];
}

type ProductionTrackType = "video" | "audio" | "music";
type ProductionMonitorSlotKey = "a" | "b";
type ProductionMonitorSlotState = {
    fragmentId: string | null;
    src: string | null;
    sourceStart: number;
};

function getProductionFragmentAtTime(
    fragments: ProductionTimelineFragment[],
    seconds: number
): ProductionTimelineFragment | null {
    if (!fragments.length) return null;
    const clampedSeconds = Math.max(0, seconds);
    for (const fragment of fragments) {
        const fragmentEnd = fragment.timeline_start + fragment.duration;
        if (clampedSeconds >= fragment.timeline_start && clampedSeconds < fragmentEnd) {
            return fragment;
        }
    }
    return fragments[fragments.length - 1] ?? null;
}

function getTrackFragments(
    fragments: ProductionTimelineFragment[],
    trackType: "video" | "music",
): ProductionTimelineFragment[] {
    return fragments
        .filter((fragment) => (fragment.track_type ?? "video") === trackType)
        .sort((left, right) => left.timeline_start - right.timeline_start);
}

function getTrackFragmentAtTime(
    fragments: ProductionTimelineFragment[],
    seconds: number,
    trackType: "video" | "music",
): ProductionTimelineFragment | null {
    return getProductionFragmentAtTime(getTrackFragments(fragments, trackType), seconds);
}

function getNextTrackFragment(
    fragments: ProductionTimelineFragment[],
    currentFragment: ProductionTimelineFragment | null,
    trackType: "video" | "music",
): ProductionTimelineFragment | null {
    if (!currentFragment) return null;
    const trackFragments = getTrackFragments(fragments, trackType);
    const currentIndex = trackFragments.findIndex((fragment) => fragment.id === currentFragment.id);
    if (currentIndex < 0) return null;
    return trackFragments[currentIndex + 1] ?? null;
}

function formatTransportTime(seconds: number): string {
    if (!Number.isFinite(seconds) || seconds <= 0) return "00:00";
    const totalSeconds = Math.max(0, Math.floor(seconds));
    const minutes = Math.floor(totalSeconds / 60);
    const remainingSeconds = totalSeconds % 60;
    return `${String(minutes).padStart(2, "0")}:${String(remainingSeconds).padStart(2, "0")}`;
}

function shouldUseChunkedAssetFetch(pathOrUrl: string): boolean {
    return /\.(mp4|m4v|mov|wav)(?:[?#].*)?$/i.test(pathOrUrl);
}

function isWaveAsset(pathOrUrl?: string | null): boolean {
    return !!pathOrUrl && /\.(wav|wave)(?:[?#].*)?$/i.test(pathOrUrl);
}

function normalizeAssetContentType(contentType: string, assetUrl: string): string {
    if (isWaveAsset(assetUrl)) {
        return "audio/wav";
    }
    return contentType;
}

function parseContentRangeTotal(contentRange: string | null): number | null {
    if (!contentRange) return null;
    const match = /^bytes\s+\d+-\d+\/(\d+)$/i.exec(contentRange.trim());
    if (!match) return null;
    const value = Number(match[1]);
    return Number.isFinite(value) && value > 0 ? value : null;
}

async function fetchAssetBlobInRanges(assetUrl: string): Promise<Blob> {
    const response = await fetch(assetUrl, {
        cache: "no-store",
        headers: {
            Range: `bytes=0-${RANGE_DOWNLOAD_CHUNK_SIZE - 1}`,
        },
    });
    if (!response.ok) {
        throw new Error(`Failed to download ${assetUrl} (${response.status})`);
    }

    const contentType = normalizeAssetContentType(
        response.headers.get("content-type") ?? "application/octet-stream",
        assetUrl,
    );
    const totalBytes = parseContentRangeTotal(response.headers.get("content-range"));
    if (response.status !== 206 || !totalBytes) {
        return new Blob([await response.arrayBuffer()], { type: contentType });
    }

    const firstChunk = await response.arrayBuffer();
    const chunks: BlobPart[] = [firstChunk];
    let downloadedBytes = firstChunk.byteLength;

    while (downloadedBytes < totalBytes) {
        const nextEnd = Math.min(downloadedBytes + RANGE_DOWNLOAD_CHUNK_SIZE - 1, totalBytes - 1);
        const chunkResponse = await fetch(assetUrl, {
            cache: "no-store",
            headers: {
                Range: `bytes=${downloadedBytes}-${nextEnd}`,
            },
        });
        if (!chunkResponse.ok) {
            throw new Error(`Failed to download ${assetUrl} (${chunkResponse.status})`);
        }
        const chunkBuffer = await chunkResponse.arrayBuffer();
        downloadedBytes += chunkBuffer.byteLength;
        chunks.push(chunkBuffer);
    }

    return new Blob(chunks, { type: contentType });
}

function normalizeMusicDurationBounds(minSeconds?: number | null, maxSeconds?: number | null): { min: number; max: number } {
    const rawMin = Number.isFinite(minSeconds) ? Number(minSeconds) : DEFAULT_MUSIC_MIN_DURATION_SECONDS;
    const rawMax = Number.isFinite(maxSeconds) ? Number(maxSeconds) : DEFAULT_MUSIC_MAX_DURATION_SECONDS;
    const min = Math.max(8, rawMin);
    const max = Math.max(8, rawMax);
    return min <= max ? { min, max } : { min: max, max: min };
}

function normalizeMusicPromptValue(value?: string | null): string {
    return (value ?? "").trim();
}

function hasCurrentGeneratedMusicTrack(
    project: ProjectState | null,
    providerId: string,
    providerUsesLyrics: boolean,
): boolean {
    if (!project?.music_url) return false;

    const bounds = normalizeMusicDurationBounds(
        project.music_min_duration_seconds,
        project.music_max_duration_seconds,
    );

    return (
        (project.generated_music_provider ?? "") === providerId
        && normalizeMusicPromptValue(project.generated_music_style_prompt) === normalizeMusicPromptValue(project.style_prompt)
        && (!providerUsesLyrics || normalizeMusicPromptValue(project.generated_music_lyrics_prompt) === normalizeMusicPromptValue(project.lyrics_prompt))
        && Number(project.generated_music_min_duration_seconds ?? NaN) === bounds.min
        && Number(project.generated_music_max_duration_seconds ?? NaN) === bounds.max
    );
}

function buildMusicPromptPackageForClipboard(project: ProjectState): string {
    const lines = [
        "Song concept",
        "",
    ];

    if (project.style_prompt?.trim()) {
        lines.push(`Style / direction: ${project.style_prompt.trim()}`, "");
    }

    if (project.lyrics_prompt?.trim()) {
        lines.push("Lyrics:", project.lyrics_prompt.trim());
    }

    return lines.join("\n").trim();
}

export default function StudioPage({ params }: { params: Promise<{ projectId: string }> }) {
    const router = useRouter();
    const { projectId } = React.use(params);
    const [project, setProject] = useState<ProjectState | null>(null);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [viewedStage, setViewedStage] = useState<string | null>(null);
    const [isRunning, setIsRunning] = useState(false);
    const [pipelineRunStatus, setPipelineRunStatus] = useState<ProjectRunStatus>({ is_running: false });
    const abortControllerRef = React.useRef<AbortController | null>(null);
    const [isSettingsOpen, setIsSettingsOpen] = useState(false);
    const [confirmDialog, setConfirmDialog] = useState<{ title: string, message: string, onConfirm: () => void } | null>(null);
    const [activeMediaSwapKey, setActiveMediaSwapKey] = useState<string | null>(null);
    const [isExportingResources, setIsExportingResources] = useState(false);
    const [isExportingFinalVideo, setIsExportingFinalVideo] = useState(false);
    const [isPreparingFinalVideo, setIsPreparingFinalVideo] = useState(false);
    const [isRegeneratingMusic, setIsRegeneratingMusic] = useState(false);
    const [isDirectorProcessing, setIsDirectorProcessing] = useState(false);
    const [storyboardAiFillClipId, setStoryboardAiFillClipId] = useState<string | null>(null);
    const [storyboardRegeneratingClipId, setStoryboardRegeneratingClipId] = useState<string | null>(null);
    const [zoomedStoryboardImage, setZoomedStoryboardImage] = useState<{ url: string; label: string } | null>(null);
    const [isAssetViewerOpen, setIsAssetViewerOpen] = useState(false);
    const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
    const [musicStageCurrentTime, setMusicStageCurrentTime] = useState(0);
    const [musicStageDuration, setMusicStageDuration] = useState(0);
    const [isMusicStagePlaying, setIsMusicStagePlaying] = useState(false);
    const [productionMusicDuration, setProductionMusicDuration] = useState(0);
    const [stagePlaybackSeconds, setStagePlaybackSeconds] = useState(0);
    const [isStagePlaybackPlaying, setIsStagePlaybackPlaying] = useState(false);

    // Stage-specific UI states
    const [selectedPlanningShotId, setSelectedPlanningShotId] = useState<string | null>(null);
    const [selectedFilmingClipId, setSelectedFilmingClipId] = useState<string | null>(null);
    const [selectedProductionFragmentId, setSelectedProductionFragmentId] = useState<string | null>(null);
    const [selectedProductionTrack, setSelectedProductionTrack] = useState<ProductionTrackType>("video");
    const [playheadSeconds, setPlayheadSeconds] = useState(0);
    const [isTimelinePlaying, setIsTimelinePlaying] = useState(false);
    const [isProductionBuffering, setIsProductionBuffering] = useState(false);
    const [activeProductionMonitorSlot, setActiveProductionMonitorSlot] = useState<ProductionMonitorSlotKey>("a");
    const [productionMonitorSlots, setProductionMonitorSlots] = useState<Record<ProductionMonitorSlotKey, ProductionMonitorSlotState>>({
        a: { fragmentId: null, src: null, sourceStart: 0 },
        b: { fragmentId: null, src: null, sourceStart: 0 },
    });

    // Storyboard Drag & Drop refs
    const dragSrc = useRef<number | null>(null);
    const dragOver = useRef<number | null>(null);
    const musicStageAudioRef = useRef<HTMLAudioElement | null>(null);
    const footerStageAudioRef = useRef<HTMLAudioElement | null>(null);
    const filmingPlaybackVideoRef = useRef<HTMLVideoElement | null>(null);
    const footerStageAudioBlobPromiseRef = useRef<Promise<string | null> | null>(null);
    const footerStageAudioBlobUrlRef = useRef<string | null>(null);
    const stageBriefAudioRef = useRef<HTMLAudioElement | null>(null);
    const knownStageBriefsRef = useRef<Record<string, string>>({});
    const pendingStageBriefAutoplayRef = useRef<string | null>(null);
    const stageBriefsHydratedRef = useRef(false);
    const productionVideoARef = useRef<HTMLVideoElement | null>(null);
    const productionVideoBRef = useRef<HTMLVideoElement | null>(null);
    const productionMusicRef = useRef<HTMLAudioElement | null>(null);
    const finalVideoBlobRef = useRef<Blob | null>(null);
    const finalVideoBlobUrlRef = useRef<string | null>(null);
    const timelineAnimationFrameRef = useRef<number | null>(null);
    const timelineLastFrameRef = useRef<number | null>(null);
    const playheadRef = useRef(0);
    const isProductionBufferingRef = useRef(false);
    const productionVideoReadyCleanupRef = useRef<(() => void) | null>(null);
    const activeProductionMonitorSlotRef = useRef<ProductionMonitorSlotKey>("a");
    const productionMonitorSlotsRef = useRef<Record<ProductionMonitorSlotKey, ProductionMonitorSlotState>>({
        a: { fragmentId: null, src: null, sourceStart: 0 },
        b: { fragmentId: null, src: null, sourceStart: 0 },
    });
    const musicStartAutosaveTimeoutRef = useRef<number | null>(null);
    const musicStartAutosaveRequestRef = useRef(0);
    const stagePlaybackFrameRef = useRef<number | null>(null);
    const stagePlaybackLastFrameRef = useRef<number | null>(null);
    const stagePlaybackSecondsRef = useRef(0);
    const [finalVideoBlobUrl, setFinalVideoBlobUrl] = useState<string | null>(null);
    const [finalVideoBlobSource, setFinalVideoBlobSource] = useState<string | null>(null);
    const [footerStageAudioBlobUrl, setFooterStageAudioBlobUrl] = useState<string | null>(null);
    const [footerStageAudioBlobSource, setFooterStageAudioBlobSource] = useState<string | null>(null);

    const resetFinalVideoPreviewCache = React.useCallback(() => {
        if (finalVideoBlobUrlRef.current) {
            URL.revokeObjectURL(finalVideoBlobUrlRef.current);
            finalVideoBlobUrlRef.current = null;
        }
        finalVideoBlobRef.current = null;
        setFinalVideoBlobUrl(null);
        setFinalVideoBlobSource(null);
    }, []);

    const getProductionVideoElement = (slot: ProductionMonitorSlotKey): HTMLVideoElement | null => (
        slot === "a" ? productionVideoARef.current : productionVideoBRef.current
    );

    const getOtherProductionMonitorSlot = (slot: ProductionMonitorSlotKey): ProductionMonitorSlotKey => (
        slot === "a" ? "b" : "a"
    );

    const updateProductionMonitorSlot = (
        slot: ProductionMonitorSlotKey,
        nextState: ProductionMonitorSlotState,
    ) => {
        const currentState = productionMonitorSlotsRef.current[slot];
        if (
            currentState.fragmentId === nextState.fragmentId
            && currentState.src === nextState.src
            && Math.abs(currentState.sourceStart - nextState.sourceStart) < 0.001
        ) {
            return;
        }

        const nextSlots = {
            ...productionMonitorSlotsRef.current,
            [slot]: nextState,
        };
        productionMonitorSlotsRef.current = nextSlots;
        setProductionMonitorSlots(nextSlots);
    };

    const replaceFooterStageAudioBlobUrl = (nextUrl: string | null, source: string | null) => {
        if (footerStageAudioBlobUrlRef.current && footerStageAudioBlobUrlRef.current !== nextUrl) {
            URL.revokeObjectURL(footerStageAudioBlobUrlRef.current);
        }
        footerStageAudioBlobUrlRef.current = nextUrl;
        setFooterStageAudioBlobUrl(nextUrl);
        setFooterStageAudioBlobSource(source);
    };

    const setActiveProductionMonitorSlotImmediate = (slot: ProductionMonitorSlotKey) => {
        const previousSlot = activeProductionMonitorSlotRef.current;
        if (previousSlot !== slot) {
            getProductionVideoElement(previousSlot)?.pause();
        }
        activeProductionMonitorSlotRef.current = slot;
        setActiveProductionMonitorSlot(slot);
    };

    const applyCurrentInputTextFields = (baseProject: ProjectState): ProjectState => {
        if (displayStage !== 'input') return baseProject;
        const screenplay = (document.getElementById('screenplay-input') as HTMLTextAreaElement | null)?.value ?? baseProject.screenplay;
        const instructions = (document.getElementById('instructions-input') as HTMLTextAreaElement | null)?.value ?? baseProject.instructions;
        const lore = (document.getElementById('lore-input') as HTMLTextAreaElement | null)?.value ?? baseProject.additional_lore;
        return {
            ...baseProject,
            screenplay,
            instructions,
            additional_lore: lore,
        };
    };

    const musicWorkflow = project?.music_workflow ?? 'lyria3';
    const shouldShowMusicPromptStage = musicWorkflow !== 'uploaded_track';
    const selectedMusicProviderId = normalizeMusicProviderId(getStoredModels().music);
    const selectedMusicProvider = getMusicProviderOption(selectedMusicProviderId);
    const usesManualMusicImport = isManualImportMusicProvider(selectedMusicProviderId);
    const stages = shouldShowMusicPromptStage
        ? ["Input", "Music Prompts", "Planning", "Storyboarding", "Filming", "Production"]
        : ["Input", "Planning", "Storyboarding", "Filming", "Production"];

    const withoutMusicStageMap: Record<string, number> = {
        'input': 0, 'planning': 1, 'storyboarding': 2, 'filming': 3, 'production': 4,
        'halted_for_review': 1, 'completed': 4
    };
    const withMusicStageMap: Record<string, number> = {
        'input': 0, 'lyria_prompting': 1, 'planning': 2, 'storyboarding': 3, 'filming': 4, 'production': 5,
        'halted_for_review': 1, 'completed': 5
    };
    const stageMap = shouldShowMusicPromptStage ? withMusicStageMap : withoutMusicStageMap;
    const stageNames = shouldShowMusicPromptStage
        ? ["input", "lyria_prompting", "planning", "storyboarding", "filming", "production"]
        : ["input", "planning", "storyboarding", "filming", "production"];
    const displayStage = viewedStage || (project?.current_stage ?? 'input');
    const resolvedCurrentReviewStage = inferReviewStageForState(project);
    const resolvedDisplayStage = displayStage === 'halted_for_review'
        ? inferReviewStageForState(project)
        : displayStage;

    const activeStageIndex = stageMap[resolvedCurrentReviewStage] ?? 0;
    const displayStageIndex = stageMap[resolvedDisplayStage] ?? 0;
    const isCurrentDisplayedStage = displayStage === (project?.current_stage ?? 'input');
    const planningMusicStartSeconds = normalizeMusicStartSeconds(project?.music_start_seconds);
    const productionTimeline = project
        ? normalizeProductionTimeline(
            project.production_timeline.length > 0
                ? (
                    project.music_url
                    && !project.production_timeline.some((fragment) => (fragment.track_type ?? "video") === "music")
                )
                    ? [
                        ...project.production_timeline,
                        ...buildDefaultProductionTimeline(project.timeline, {
                            includeMusic: true,
                            musicDuration: productionMusicDuration || undefined,
                            musicStartSeconds: planningMusicStartSeconds,
                        }).filter((fragment) => fragment.track_type === "music"),
                    ]
                    : project.production_timeline
                : buildDefaultProductionTimeline(project.timeline, {
                    includeMusic: !!project.music_url,
                    musicDuration: productionMusicDuration || undefined,
                    musicStartSeconds: planningMusicStartSeconds,
                }),
            project.timeline,
            productionMusicDuration || undefined,
        )
        : [];
    const selectedAsset = project
        ? project.assets.find((asset) => asset.id === selectedAssetId) ?? project.assets[0] ?? null
        : null;
    const liveDirectorFocusLabel = (() => {
        if (!project) return null;
        if (isAssetViewerOpen && selectedAsset) {
            return `Asset: ${selectedAsset.label?.trim() || selectedAsset.name}`;
        }
        if (displayStage === 'planning' && selectedPlanningShotId) {
            const shotIndex = project.timeline.findIndex((clip) => clip.id === selectedPlanningShotId);
            return shotIndex >= 0 ? `Shot ${shotIndex + 1}` : "Selected shot";
        }
        if (displayStage === 'storyboarding' && selectedPlanningShotId) {
            const shotIndex = project.timeline.findIndex((clip) => clip.id === selectedPlanningShotId);
            return shotIndex >= 0 ? `Shot ${shotIndex + 1}` : "Selected frame";
        }
        if (displayStage === 'filming' && selectedFilmingClipId) {
            const shotIndex = project.timeline.findIndex((clip) => clip.id === selectedFilmingClipId);
            return shotIndex >= 0 ? `Clip ${shotIndex + 1}` : "Selected clip";
        }
        if (displayStage === 'production' && selectedProductionFragmentId) {
            const fragment = productionTimeline.find((item) => item.id === selectedProductionFragmentId);
            if (!fragment) return "Selected edit";
            if ((fragment.track_type ?? "video") === "music") {
                return "Selected music segment";
            }
            const shotIndex = project.timeline.findIndex((clip) => clip.id === fragment.source_clip_id);
            return shotIndex >= 0 ? `Edit from Shot ${shotIndex + 1}` : "Selected edit";
        }
        return null;
    })();
    const isProductionDisplay = displayStage === 'production';
    const stageSummary = project?.stage_summaries?.[displayStage] ?? null;
    const stageVoiceBriefsEnabled = getStoredPreferences().stageVoiceBriefsEnabled;
    const shouldAutoplayStageBrief = !!project
        && displayStage === project.current_stage
        && stageVoiceBriefsEnabled
        && !!stageSummary?.audio_url;
    const hasExportableResources = !!project && (
        !!project.music_url
        || project.timeline.some((clip) => !!clip.video_url || !!clip.image_url)
    );
    const productionDuration = productionTimeline.reduce(
        (maxDuration, fragment) => Math.max(maxDuration, fragment.timeline_start + fragment.duration),
        0
    );
    const stageTimelineDuration = Math.max(
        project?.timeline.reduce(
            (maxDuration, clip) => Math.max(maxDuration, clip.timeline_start + clip.duration),
            0,
        ) ?? 0,
        project?.music_url
            ? planningMusicStartSeconds + Math.max(0, productionMusicDuration || 0)
            : 0,
    );
    const supportsFooterStagePlayback = displayStage === "input"
        || displayStage === "planning"
        || displayStage === "storyboarding"
        || displayStage === "filming";
    const hasFooterStageContent = !!project?.music_url || (project?.timeline.length ?? 0) > 0;
    const shouldShowFooter = isAssetViewerOpen
        || displayStage === "production"
        || displayStage === "lyria_prompting"
        || !supportsFooterStagePlayback
        || hasFooterStageContent;
    const canControlFooterStagePlayback = supportsFooterStagePlayback && !!project?.music_url && stageTimelineDuration > 0;
    const storyboardPlaybackClip = displayStage === "storyboarding"
        ? getTimelineClipAtTime(project?.timeline ?? [], stagePlaybackSeconds)
        : null;
    const filmingPlaybackClip = displayStage === "filming"
        ? getTimelineClipAtTime(project?.timeline ?? [], stagePlaybackSeconds)
        : null;
    const activeProductionFragment = getTrackFragmentAtTime(productionTimeline, playheadSeconds, "video");
    const activeProductionClip = project && activeProductionFragment
        ? project.timeline.find((clip) => clip.id === activeProductionFragment.source_clip_id) ?? null
        : null;
    const nextProductionFragment = getNextTrackFragment(productionTimeline, activeProductionFragment, "video");
    const activeProductionMonitor = productionMonitorSlots[activeProductionMonitorSlot];
    const isPipelineRunning = pipelineRunStatus.is_running;
    const isBusy = isRunning || isPipelineRunning || isRegeneratingMusic;
    const isStoryboardingRunActive = isPipelineRunning && project?.current_stage === 'storyboarding';
    const isFilmingRunActive = isPipelineRunning && project?.current_stage === 'filming';
    const readyStoryboardClipCount = project?.timeline.filter((clip) => !!clip.image_url).length ?? 0;
    const totalStoryboardClipCount = project?.timeline.length ?? 0;
    const readyFilmingClipCount = project?.timeline.filter((clip) => !!clip.video_url).length ?? 0;
    const totalFilmingClipCount = project?.timeline.length ?? 0;
    const musicDurationBounds = normalizeMusicDurationBounds(
        project?.music_min_duration_seconds,
        project?.music_max_duration_seconds,
    );
    const hasCurrentAutomaticMusicTrack = hasCurrentGeneratedMusicTrack(
        project,
        selectedMusicProviderId,
        selectedMusicProvider.usesLyrics,
    );

    useEffect(() => {
        Promise.all([api.getProject(projectId), api.getRunStatus(projectId)])
            .then(([projectData, runStatus]) => {
                setProject(projectData);
                setPipelineRunStatus(runStatus);
                setLoadError(null);
            })
            .catch((error) => {
                const message = error instanceof Error ? error.message : "Failed to load project.";
                setLoadError(message);
            });
    }, [projectId]);

    useEffect(() => {
        if (!isAssetViewerOpen || !project) return;
        if (selectedAssetId && project.assets.some((asset) => asset.id === selectedAssetId)) {
            return;
        }
        setSelectedAssetId(project.assets[0]?.id ?? null);
    }, [isAssetViewerOpen, project, selectedAssetId]);

    useEffect(() => {
        if (!isAssetViewerOpen) return;
        setIsTimelinePlaying(false);
        setIsStagePlaybackPlaying(false);
        setIsMusicStagePlaying(false);
        productionVideoARef.current?.pause();
        productionVideoBRef.current?.pause();
        productionMusicRef.current?.pause();
        musicStageAudioRef.current?.pause();
        footerStageAudioRef.current?.pause();
    }, [isAssetViewerOpen]);

    useEffect(() => () => {
        if (musicStartAutosaveTimeoutRef.current !== null) {
            window.clearTimeout(musicStartAutosaveTimeoutRef.current);
            musicStartAutosaveTimeoutRef.current = null;
        }
    }, []);

    useEffect(() => {
        knownStageBriefsRef.current = {};
        pendingStageBriefAutoplayRef.current = null;
        stageBriefsHydratedRef.current = false;
    }, [projectId]);

    useEffect(() => {
        if (!isPipelineRunning) return;

        let cancelled = false;
        const poll = async () => {
            try {
                const [projectData, runStatus] = await Promise.all([
                    api.getProject(projectId),
                    api.getRunStatus(projectId),
                ]);
                if (cancelled) return;

                setProject(projectData);
                setPipelineRunStatus(runStatus);
                setLoadError(null);
            } catch (error) {
                if (cancelled) return;
                console.error("Failed to refresh live pipeline state", error);
            }
        };

        void poll();
        const intervalId = window.setInterval(() => {
            void poll();
        }, 3000);

        return () => {
            cancelled = true;
            window.clearInterval(intervalId);
        };
    }, [isPipelineRunning, projectId]);

    useEffect(() => {
        if (!project) return;

        const summaries = project.stage_summaries ?? {};
        if (!stageBriefsHydratedRef.current) {
            knownStageBriefsRef.current = Object.fromEntries(
                Object.entries(summaries)
                    .filter(([, summary]) => !!summary?.generated_at)
                    .map(([stageName, summary]) => [stageName, summary.generated_at]),
            );
            stageBriefsHydratedRef.current = true;
            return;
        }

        let nextPendingAutoplayKey: string | null = null;
        for (const [stageName, summary] of Object.entries(summaries)) {
            const generatedAt = summary?.generated_at;
            if (!generatedAt) continue;

            const previousGeneratedAt = knownStageBriefsRef.current[stageName];
            if (previousGeneratedAt === generatedAt) continue;

            if (
                stageName === project.current_stage
                && displayStage === project.current_stage
                && stageVoiceBriefsEnabled
                && summary.audio_url
            ) {
                nextPendingAutoplayKey = `${projectId}:${stageName}:${generatedAt}`;
            }

            knownStageBriefsRef.current[stageName] = generatedAt;
        }

        if (nextPendingAutoplayKey) {
            pendingStageBriefAutoplayRef.current = nextPendingAutoplayKey;
        }
    }, [
        displayStage,
        project,
        projectId,
        stageVoiceBriefsEnabled,
    ]);

    useEffect(() => {
        const audio = stageBriefAudioRef.current;
        if (!audio) return;

        if (!shouldAutoplayStageBrief || !stageSummary?.audio_url) {
            audio.pause();
            audio.currentTime = 0;
            return;
        }

        const autoplayKey = `${projectId}:${displayStage}:${stageSummary.generated_at}`;
        if (pendingStageBriefAutoplayRef.current !== autoplayKey) {
            return;
        }

        audio.pause();
        audio.currentTime = 0;
        pendingStageBriefAutoplayRef.current = null;
        void audio.play().catch((error) => {
            console.error("Stage brief autoplay failed", error);
        });
    }, [
        displayStage,
        project?.current_stage,
        projectId,
        shouldAutoplayStageBrief,
        stageSummary?.audio_url,
        stageSummary?.generated_at,
    ]);

    useEffect(() => {
        return () => {
            resetFinalVideoPreviewCache();
            if (footerStageAudioBlobUrlRef.current) {
                URL.revokeObjectURL(footerStageAudioBlobUrlRef.current);
                footerStageAudioBlobUrlRef.current = null;
            }
        };
    }, [resetFinalVideoPreviewCache]);

    useEffect(() => {
        if (displayStage === "completed" && project?.final_video_url) {
            return;
        }
        if (
            !finalVideoBlobRef.current
            && !finalVideoBlobUrlRef.current
            && !finalVideoBlobSource
            && !finalVideoBlobUrl
        ) {
            return;
        }
        resetFinalVideoPreviewCache();
    }, [
        displayStage,
        finalVideoBlobSource,
        finalVideoBlobUrl,
        project?.final_video_url,
        resetFinalVideoPreviewCache,
    ]);

    useEffect(() => {
        if (displayStage !== "completed" || !project?.final_video_url) {
            setIsPreparingFinalVideo(false);
            return;
        }
        if (finalVideoBlobSource === project.final_video_url && finalVideoBlobUrl) {
            return;
        }

        let cancelled = false;
        setIsPreparingFinalVideo(true);

        void loadFinalVideoBlob(project.final_video_url)
            .then((blob) => {
                if (cancelled) return;
                const objectUrl = URL.createObjectURL(blob);
                if (finalVideoBlobUrlRef.current) {
                    URL.revokeObjectURL(finalVideoBlobUrlRef.current);
                }
                finalVideoBlobUrlRef.current = objectUrl;
                setFinalVideoBlobUrl(objectUrl);
                setFinalVideoBlobSource(project.final_video_url ?? null);
            })
            .catch((error) => {
                if (cancelled) return;
                console.error("Failed to prepare final video preview", error);
                resetFinalVideoPreviewCache();
            })
            .finally(() => {
                if (!cancelled) {
                    setIsPreparingFinalVideo(false);
                }
            });

        return () => {
            cancelled = true;
        };
    }, [displayStage, finalVideoBlobSource, finalVideoBlobUrl, project?.final_video_url, resetFinalVideoPreviewCache]);

    useEffect(() => {
        if (displayStage !== 'filming') return;
        setSelectedFilmingClipId(null);
    }, [displayStage]);

    useEffect(() => {
        if (displayStage !== 'filming' || !project || !selectedFilmingClipId) return;
        if (project.timeline.some((clip) => clip.id === selectedFilmingClipId)) return;
        setSelectedFilmingClipId(null);
    }, [displayStage, project, selectedFilmingClipId]);

    const syncMusicStagePlaybackState = () => {
        const audio = musicStageAudioRef.current;
        if (!audio) {
            setMusicStageCurrentTime(0);
            setMusicStageDuration(0);
            setIsMusicStagePlaying(false);
            return;
        }

        setMusicStageCurrentTime(audio.currentTime || 0);
        setMusicStageDuration(Number.isFinite(audio.duration) ? audio.duration : 0);
        setIsMusicStagePlaying(!audio.paused && !audio.ended);
    };

    useEffect(() => {
        if (displayStage === 'lyria_prompting') return;
        const audio = musicStageAudioRef.current;
        if (!audio) return;
        audio.pause();
        audio.currentTime = 0;
        setMusicStageCurrentTime(0);
        setIsMusicStagePlaying(false);
    }, [displayStage]);

    useEffect(() => {
        setMusicStageCurrentTime(0);
        setMusicStageDuration(0);
        setIsMusicStagePlaying(false);
    }, [project?.music_url]);

    useEffect(() => {
        footerStageAudioBlobPromiseRef.current = null;
        if (!project?.music_url || !isWaveAsset(project.music_url)) {
            replaceFooterStageAudioBlobUrl(null, null);
            return;
        }

        replaceFooterStageAudioBlobUrl(null, null);
        void ensureFooterStageAudioSourceLoaded();
    }, [project?.music_url]);

    useEffect(() => {
        const audio = footerStageAudioRef.current;
        if (audio) {
            audio.pause();
            audio.currentTime = 0;
            audio.muted = false;
        }
    }, [project?.music_url, footerStageAudioBlobUrl]);

    useEffect(() => {
        stagePlaybackSecondsRef.current = stagePlaybackSeconds;
    }, [stagePlaybackSeconds]);

    useEffect(() => {
        if (supportsFooterStagePlayback) return;
        if (stagePlaybackFrameRef.current !== null) {
            cancelAnimationFrame(stagePlaybackFrameRef.current);
            stagePlaybackFrameRef.current = null;
        }
        stagePlaybackLastFrameRef.current = null;
        setIsStagePlaybackPlaying(false);
        setStagePlaybackSeconds(0);
        stagePlaybackSecondsRef.current = 0;
        const audio = footerStageAudioRef.current;
        if (audio) {
            audio.pause();
            audio.currentTime = 0;
            audio.muted = false;
        }
    }, [supportsFooterStagePlayback, project?.music_url]);

    useEffect(() => {
        if (!supportsFooterStagePlayback || !hasFooterStageContent) return;
        if (stagePlaybackSecondsRef.current > stageTimelineDuration) {
            handleFooterStageSeek(stageTimelineDuration);
        }
    }, [supportsFooterStagePlayback, hasFooterStageContent, stageTimelineDuration]);

    useEffect(() => {
        if (!supportsFooterStagePlayback) return;
        if (!isStagePlaybackPlaying) {
            startFooterStageAudioAtTimelineSeconds(stagePlaybackSecondsRef.current, false);
            return;
        }

        const step = (timestamp: number) => {
            if (stagePlaybackLastFrameRef.current === null) {
                stagePlaybackLastFrameRef.current = timestamp;
            }

            const previousSeconds = stagePlaybackSecondsRef.current;
            const deltaSeconds = (timestamp - (stagePlaybackLastFrameRef.current ?? timestamp)) / 1000;
            stagePlaybackLastFrameRef.current = timestamp;

            const audio = footerStageAudioRef.current;
            const audioIsDrivingPlayback = !!audio
                && !audio.paused
                && !audio.ended
                && previousSeconds >= planningMusicStartSeconds - 0.05;

            let nextSeconds = previousSeconds;
            if (audioIsDrivingPlayback) {
                nextSeconds = Math.min(
                    stageTimelineDuration,
                    planningMusicStartSeconds + Math.max(0, audio.currentTime || 0),
                );
            } else {
                nextSeconds = Math.min(stageTimelineDuration, previousSeconds + deltaSeconds);
                if (previousSeconds < planningMusicStartSeconds && nextSeconds >= planningMusicStartSeconds) {
                    startFooterStageAudioAtTimelineSeconds(nextSeconds, true);
                }
            }

            stagePlaybackSecondsRef.current = nextSeconds;
            setStagePlaybackSeconds(nextSeconds);

            if (nextSeconds >= stageTimelineDuration) {
                setIsStagePlaybackPlaying(false);
                if (audio) {
                    audio.pause();
                    audio.muted = false;
                }
                stagePlaybackFrameRef.current = null;
                stagePlaybackLastFrameRef.current = null;
                return;
            }

            stagePlaybackFrameRef.current = requestAnimationFrame(step);
        };

        stagePlaybackFrameRef.current = requestAnimationFrame(step);
        return () => {
            if (stagePlaybackFrameRef.current !== null) {
                cancelAnimationFrame(stagePlaybackFrameRef.current);
                stagePlaybackFrameRef.current = null;
            }
            stagePlaybackLastFrameRef.current = null;
        };
    }, [supportsFooterStagePlayback, isStagePlaybackPlaying, stageTimelineDuration, planningMusicStartSeconds]);

    useEffect(() => {
        if (displayStage !== "filming") {
            filmingPlaybackVideoRef.current?.pause();
            return;
        }

        const video = filmingPlaybackVideoRef.current;
        if (!video || !filmingPlaybackClip?.video_url) {
            video?.pause();
            return;
        }

        const clipOffsetSeconds = Math.max(0, stagePlaybackSeconds - filmingPlaybackClip.timeline_start);
        if (Math.abs(video.currentTime - clipOffsetSeconds) > 0.35) {
            try {
                video.currentTime = clipOffsetSeconds;
            } catch {
                // Ignore seek errors until metadata is ready.
            }
        }

        if (isStagePlaybackPlaying) {
            void video.play().catch((error) => {
                console.error("Filming sequence playback failed", error);
            });
        } else {
            video.pause();
        }
    }, [
        displayStage,
        filmingPlaybackClip?.id,
        filmingPlaybackClip?.timeline_start,
        filmingPlaybackClip?.video_url,
        isStagePlaybackPlaying,
        stagePlaybackSeconds,
    ]);

    const selectedProductionFragment = productionTimeline.find((fragment) => fragment.id === selectedProductionFragmentId) ?? null;
    const canSplitSelected = !!selectedProductionFragment
        && playheadSeconds > selectedProductionFragment.timeline_start + MIN_PRODUCTION_FRAGMENT_DURATION
        && playheadSeconds < (selectedProductionFragment.timeline_start + selectedProductionFragment.duration - MIN_PRODUCTION_FRAGMENT_DURATION);
    const canToggleSelectedAudio = !!selectedProductionFragment
        && (selectedProductionFragment.track_type ?? "video") === "video"
        && selectedProductionTrack === "audio";
    const canControlMusicStagePreview = displayStage === 'lyria_prompting' && !!project?.music_url;
    const musicTrackNeedsRegeneration = !usesManualMusicImport && !!project?.music_url && !hasCurrentAutomaticMusicTrack;
    const canContinueFromMusicStage = usesManualMusicImport ? !!project?.music_url : hasCurrentAutomaticMusicTrack;

    const handleToggleMusicStagePlayback = async () => {
        const audio = musicStageAudioRef.current;
        if (!audio || !project?.music_url) return;

        try {
            if (audio.paused) {
                await audio.play();
            } else {
                audio.pause();
            }
        } catch (error) {
            console.error("Music preview playback failed", error);
        }
        syncMusicStagePlaybackState();
    };

    const handleJumpMusicStagePlayback = (deltaSeconds: number) => {
        const audio = musicStageAudioRef.current;
        if (!audio) return;
        const duration = Number.isFinite(audio.duration) ? audio.duration : musicStageDuration;
        const nextTime = Math.max(0, Math.min((duration || 0), audio.currentTime + deltaSeconds));
        audio.currentTime = nextTime;
        syncMusicStagePlaybackState();
    };

    const handleSeekMusicStagePlayback = (seconds: number) => {
        const audio = musicStageAudioRef.current;
        if (!audio) return;
        audio.currentTime = seconds;
        syncMusicStagePlaybackState();
    };

    const musicStageStatusLabel = usesManualMusicImport
        ? (project?.music_url ? "Song Imported" : "Song Needed")
        : hasCurrentAutomaticMusicTrack
            ? "Song Ready"
            : project?.music_url
                ? "Song Outdated"
                : "Song Needed";

    const handleMusicDurationBoundChange = (
        field: "music_min_duration_seconds" | "music_max_duration_seconds",
        rawValue: string,
    ) => {
        if (!project) return;

        const trimmed = rawValue.trim();
        const nextValue = trimmed === "" ? undefined : Number(trimmed);
        setProject({
            ...project,
            [field]: Number.isFinite(nextValue) ? nextValue : undefined,
        });
    };

    const handlePlanningMusicStartChange = (rawValue: string) => {
        if (!project) return;

        const trimmed = rawValue.trim();
        const nextMusicStartSeconds = trimmed === ""
            ? DEFAULT_MUSIC_START_SECONDS
            : normalizeMusicStartSeconds(Number(trimmed));
        const videoFragments = project.production_timeline.filter(
            (fragment) => (fragment.track_type ?? "video") !== "music",
        );
        const nextProductionTimeline = videoFragments.length > 0
            ? [
                ...videoFragments,
                ...(
                    project.music_url
                        ? buildDefaultProductionTimeline(project.timeline, {
                            includeMusic: true,
                            musicDuration: productionMusicDuration || undefined,
                            musicStartSeconds: nextMusicStartSeconds,
                        }).filter((fragment) => fragment.track_type === "music")
                        : []
                ),
            ]
            : buildDefaultProductionTimeline(project.timeline, {
                includeMusic: !!project.music_url,
                musicDuration: productionMusicDuration || undefined,
                musicStartSeconds: nextMusicStartSeconds,
            });

        const nextProject = {
            ...project,
            music_start_seconds: nextMusicStartSeconds,
            production_timeline: nextProductionTimeline,
            final_video_url: undefined,
            last_error: undefined,
        };
        setProject(nextProject);

        if (musicStartAutosaveTimeoutRef.current !== null) {
            window.clearTimeout(musicStartAutosaveTimeoutRef.current);
        }
        const requestId = musicStartAutosaveRequestRef.current + 1;
        musicStartAutosaveRequestRef.current = requestId;
        musicStartAutosaveTimeoutRef.current = window.setTimeout(async () => {
            try {
                const savedProject = await api.updateProject(nextProject.project_id, nextProject);
                if (musicStartAutosaveRequestRef.current !== requestId) return;
                setProject(savedProject);
            } catch (error) {
                if (musicStartAutosaveRequestRef.current !== requestId) return;
                console.error("Failed to autosave music start", error);
                alert("Failed to save music start.");
            } finally {
                if (musicStartAutosaveRequestRef.current === requestId) {
                    musicStartAutosaveTimeoutRef.current = null;
                }
            }
        }, 450);
    };

    const ensureFooterStageAudioSourceLoaded = async (): Promise<string | null> => {
        if (!project?.music_url) return null;

        const directUrl = toBackendAssetUrl(project.music_url);
        if (!isWaveAsset(project.music_url)) {
            return directUrl;
        }

        if (footerStageAudioBlobSource === project.music_url && footerStageAudioBlobUrl) {
            return footerStageAudioBlobUrl;
        }

        if (!footerStageAudioBlobPromiseRef.current) {
            const sourcePath = project.music_url;
            footerStageAudioBlobPromiseRef.current = fetchAssetBlobInRanges(directUrl)
                .then((blob) => {
                    if (project?.music_url !== sourcePath) {
                        return null;
                    }
                    const objectUrl = URL.createObjectURL(blob);
                    replaceFooterStageAudioBlobUrl(objectUrl, sourcePath);
                    return objectUrl;
                })
                .catch((error) => {
                    console.error("Failed to preload footer stage audio", error);
                    return directUrl;
                })
                .finally(() => {
                    footerStageAudioBlobPromiseRef.current = null;
                });
        }

        return footerStageAudioBlobPromiseRef.current;
    };

    const getFooterStageAudioSourceUrl = (): string => {
        if (!project?.music_url) return "";
        if (footerStageAudioBlobSource === project.music_url && footerStageAudioBlobUrl) {
            return footerStageAudioBlobUrl;
        }
        return toBackendAssetUrl(project.music_url);
    };

    const startFooterStageAudioAtTimelineSeconds = (seconds: number, autoplay: boolean) => {
        const audio = footerStageAudioRef.current;
        if (!audio || !project?.music_url) return;

        const knownDuration = Number.isFinite(audio.duration) ? audio.duration : productionMusicDuration;
        const musicRelativeSeconds = seconds - planningMusicStartSeconds;

        if (musicRelativeSeconds < 0) {
            audio.pause();
            if (Math.abs(audio.currentTime) > 0.2) {
                audio.currentTime = 0;
            }
            audio.muted = false;
            return;
        }

        if (knownDuration && musicRelativeSeconds >= knownDuration) {
            audio.pause();
            audio.muted = false;
            return;
        }

        const clampedMusicSeconds = knownDuration
            ? Math.max(0, Math.min(musicRelativeSeconds, knownDuration))
            : Math.max(0, musicRelativeSeconds);
        if (Math.abs(audio.currentTime - clampedMusicSeconds) > 0.35) {
            audio.currentTime = clampedMusicSeconds;
        }
        audio.muted = false;
        if (autoplay) {
            void audio.play().catch((error) => {
                console.error("Footer stage playback failed", error);
            });
        } else {
            audio.pause();
        }
    };

    const handleFooterStageSeek = (seconds: number) => {
        const clampedSeconds = Math.max(0, Math.min(seconds, stageTimelineDuration));
        stagePlaybackSecondsRef.current = clampedSeconds;
        setStagePlaybackSeconds(clampedSeconds);
        startFooterStageAudioAtTimelineSeconds(clampedSeconds, isStagePlaybackPlaying);
    };

    const handleToggleFooterStagePlayback = async () => {
        if (!canControlFooterStagePlayback) return;

        if (isStagePlaybackPlaying) {
            setIsStagePlaybackPlaying(false);
            return;
        }

        const resolvedSource = await ensureFooterStageAudioSourceLoaded();
        const audio = footerStageAudioRef.current;
        if (audio && resolvedSource && audio.src !== resolvedSource) {
            audio.src = resolvedSource;
            audio.load();
        }
        if (stagePlaybackSecondsRef.current >= stageTimelineDuration) {
            handleFooterStageSeek(0);
        }
        setIsStagePlaybackPlaying(true);
        startFooterStageAudioAtTimelineSeconds(stagePlaybackSecondsRef.current, true);
    };

    const handleJumpFooterStagePlayback = (deltaSeconds: number) => {
        if (!canControlFooterStagePlayback) return;
        handleFooterStageSeek(stagePlaybackSecondsRef.current + deltaSeconds);
    };

    const primeProductionMonitorSlot = (
        slot: ProductionMonitorSlotKey,
        fragment: ProductionTimelineFragment | null,
        clip: VideoClip | null,
    ) => {
        if (!fragment || !clip?.video_url) {
            updateProductionMonitorSlot(slot, { fragmentId: null, src: null, sourceStart: 0 });
            return;
        }

        updateProductionMonitorSlot(slot, {
            fragmentId: fragment.id,
            src: toBackendAssetUrl(clip.video_url),
            sourceStart: fragment.source_start,
        });
    };

    const handleProductionMonitorSlotLoadedMetadata = (slot: ProductionMonitorSlotKey) => {
        const slotState = productionMonitorSlotsRef.current[slot];
        const videoElement = getProductionVideoElement(slot);
        if (!slotState.fragmentId || !videoElement) return;

        const drift = Math.abs(videoElement.currentTime - slotState.sourceStart);
        if (drift > 0.05) {
            try {
                videoElement.currentTime = slotState.sourceStart;
            } catch {
                // Ignore seek timing errors while the browser is still loading.
            }
        }

        if (slot === activeProductionMonitorSlotRef.current && !isTimelinePlaying) {
            syncProductionMedia(playheadRef.current, false);
        }
    };

    const handleProductionMonitorSlotCanPlay = (slot: ProductionMonitorSlotKey) => {
        handleProductionMonitorSlotLoadedMetadata(slot);
        if (slot !== activeProductionMonitorSlotRef.current) return;
        if (!isProductionBufferingRef.current) return;

        isProductionBufferingRef.current = false;
        setIsProductionBuffering(false);
        syncProductionMedia(playheadRef.current, isTimelinePlaying);
    };

    const syncProductionMedia = (seconds: number, autoplay: boolean) => {
        if (!project || !isProductionDisplay) return;

        const clampedSeconds = Math.max(0, Math.min(seconds, productionDuration));
        const fragment = getTrackFragmentAtTime(productionTimeline, clampedSeconds, "video");
        const musicFragment = getTrackFragmentAtTime(productionTimeline, clampedSeconds, "music");
        const clip = fragment
            ? project.timeline.find((timelineClip) => timelineClip.id === fragment.source_clip_id) ?? null
            : null;
        const nextFragment = getNextTrackFragment(productionTimeline, fragment, "video");
        const nextClip = nextFragment
            ? project.timeline.find((timelineClip) => timelineClip.id === nextFragment.source_clip_id) ?? null
            : null;

        const musicElement = productionMusicRef.current;
        const syncMusicElement = (shouldPlay: boolean) => {
            if (!musicElement || !project.music_url) return;
            if (musicFragment) {
                const musicSeconds = musicFragment.source_start + Math.max(0, clampedSeconds - musicFragment.timeline_start);
                const drift = Math.abs(musicElement.currentTime - musicSeconds);
                if (drift > 0.25) {
                    musicElement.currentTime = musicSeconds;
                }
                if (shouldPlay) {
                    void musicElement.play().catch(() => {});
                } else {
                    musicElement.pause();
                }
            } else {
                musicElement.pause();
            }
        };

        let activeSlot = activeProductionMonitorSlotRef.current;
        let inactiveSlot = getOtherProductionMonitorSlot(activeSlot);
        const slotStates = productionMonitorSlotsRef.current;

        let primedCurrentFragmentIntoSlot = false;
        if (fragment && clip?.video_url) {
            if (slotStates[activeSlot].fragmentId !== fragment.id) {
                if (slotStates[inactiveSlot].fragmentId === fragment.id) {
                    activeSlot = inactiveSlot;
                    inactiveSlot = getOtherProductionMonitorSlot(activeSlot);
                    setActiveProductionMonitorSlotImmediate(activeSlot);
                } else {
                    primeProductionMonitorSlot(activeSlot, fragment, clip);
                    primedCurrentFragmentIntoSlot = true;
                }
            }
            primeProductionMonitorSlot(inactiveSlot, nextFragment, nextClip);
        } else {
            primeProductionMonitorSlot(activeSlot, null, null);
            primeProductionMonitorSlot(inactiveSlot, null, null);
        }

        const videoElement = getProductionVideoElement(activeSlot);
        if (!videoElement || !fragment || !clip?.video_url) {
            productionVideoReadyCleanupRef.current?.();
            productionVideoReadyCleanupRef.current = null;
            isProductionBufferingRef.current = false;
            setIsProductionBuffering(false);
            videoElement?.pause();
            syncMusicElement(autoplay);
            return;
        }
        videoElement.muted = !(fragment.audio_enabled ?? true);

        if (primedCurrentFragmentIntoSlot) {
            if (autoplay) {
                isProductionBufferingRef.current = true;
                setIsProductionBuffering(true);
                syncMusicElement(false);
            }
            return;
        }

        const sourceSeconds = fragment.source_start + Math.max(0, clampedSeconds - fragment.timeline_start);
        const applyVideoSync = () => {
            const drift = Math.abs(videoElement.currentTime - sourceSeconds);
            if (drift > 0.2) {
                videoElement.currentTime = sourceSeconds;
            }
            if (autoplay) {
                void videoElement.play().catch(() => {});
            } else {
                videoElement.pause();
            }
        };

        const requiredReadyState = autoplay ? HTMLMediaElement.HAVE_FUTURE_DATA : HTMLMediaElement.HAVE_METADATA;
        if (videoElement.readyState >= requiredReadyState) {
            productionVideoReadyCleanupRef.current?.();
            productionVideoReadyCleanupRef.current = null;
            isProductionBufferingRef.current = false;
            setIsProductionBuffering(false);
            applyVideoSync();
            syncMusicElement(autoplay);
            return;
        }

        productionVideoReadyCleanupRef.current?.();
        productionVideoReadyCleanupRef.current = null;
        if (autoplay) {
            isProductionBufferingRef.current = true;
            setIsProductionBuffering(true);
            videoElement.pause();
            syncMusicElement(false);
        }

        const readyEvent = autoplay ? "canplay" : "loadedmetadata";
        const handleVideoReady = () => {
            productionVideoReadyCleanupRef.current?.();
            productionVideoReadyCleanupRef.current = null;
            isProductionBufferingRef.current = false;
            setIsProductionBuffering(false);
            applyVideoSync();
            syncMusicElement(autoplay);
        };
        videoElement.addEventListener(readyEvent, handleVideoReady, { once: true });
        productionVideoReadyCleanupRef.current = () => {
            videoElement.removeEventListener(readyEvent, handleVideoReady);
        };
    };

    const handleProductionSeek = (seconds: number) => {
        const clampedSeconds = Math.max(0, Math.min(seconds, productionDuration));
        const activeFragment = getTrackFragmentAtTime(productionTimeline, clampedSeconds, "video");
        setSelectedProductionFragmentId(activeFragment?.id ?? null);
        playheadRef.current = clampedSeconds;
        setPlayheadSeconds(clampedSeconds);
        syncProductionMedia(clampedSeconds, isTimelinePlaying);
    };

    const updateProductionTimeline = (
        fragments: ProductionTimelineFragment[],
        options?: {
            selectedFragmentId?: string | null;
            selectedTrack?: ProductionTrackType;
        }
    ) => {
        if (!project) return;
        const normalizedTimeline = normalizeProductionTimeline(
            fragments,
            project.timeline,
            productionMusicDuration || undefined,
        );
        const nextProject: ProjectState = {
            ...project,
            current_stage: 'production',
            production_timeline: normalizedTimeline,
            final_video_url: undefined,
            last_error: undefined,
        };
        setProject(nextProject);

        const requestedFragmentId = options?.selectedFragmentId ?? selectedProductionFragmentId;
        const nextSelectedFragmentId = requestedFragmentId && normalizedTimeline.find((fragment) => fragment.id === requestedFragmentId)
            ? requestedFragmentId
            : normalizedTimeline[0]?.id ?? null;
        setSelectedProductionFragmentId(nextSelectedFragmentId);
        if (options?.selectedTrack) {
            setSelectedProductionTrack(options.selectedTrack);
        }

        const nextDuration = normalizedTimeline.reduce(
            (maxDuration, fragment) => Math.max(maxDuration, fragment.timeline_start + fragment.duration),
            0
        );
        if (playheadRef.current > nextDuration) {
            handleProductionSeek(nextDuration);
        }
    };

    const handleSelectProductionFragment = (fragmentId: string, track: ProductionTrackType) => {
        setSelectedProductionFragmentId(fragmentId);
        setSelectedProductionTrack(track);
    };

    const handleMoveVideoProductionFragment = (draggedFragmentId: string, beforeFragmentId: string | null) => {
        if (displayStage !== 'production' || isBusy) return;
        if (beforeFragmentId === draggedFragmentId) return;
        const videoFragments = getTrackFragments(productionTimeline, "video");
        const musicFragments = getTrackFragments(productionTimeline, "music");
        const reorderedTimeline = [...videoFragments];
        const draggedIndex = reorderedTimeline.findIndex((fragment) => fragment.id === draggedFragmentId);
        if (draggedIndex === -1) return;

        const [draggedFragment] = reorderedTimeline.splice(draggedIndex, 1);
        let targetIndex = beforeFragmentId
            ? reorderedTimeline.findIndex((fragment) => fragment.id === beforeFragmentId)
            : reorderedTimeline.length;
        if (targetIndex < 0) targetIndex = reorderedTimeline.length;
        reorderedTimeline.splice(targetIndex, 0, draggedFragment);
        updateProductionTimeline([...reorderedTimeline, ...musicFragments], {
            selectedFragmentId: draggedFragmentId,
            selectedTrack: "video",
        });
    };

    const handleMoveMusicProductionFragment = (draggedFragmentId: string, timelineStartSeconds: number) => {
        if (displayStage !== 'production' || isBusy) return;
        const videoFragments = getTrackFragments(productionTimeline, "video");
        const musicFragments = getTrackFragments(productionTimeline, "music");
        const movedTimeline = musicFragments.map((fragment) =>
            fragment.id === draggedFragmentId
                ? {
                    ...fragment,
                    timeline_start: Math.max(0, Number(timelineStartSeconds.toFixed(3))),
                }
                : fragment
        );
        updateProductionTimeline([...videoFragments, ...movedTimeline], {
            selectedFragmentId: draggedFragmentId,
            selectedTrack: "music",
        });
    };

    const handleSplitProductionFragment = () => {
        if (displayStage !== 'production' || isBusy) return;
        if (!selectedProductionFragment || !canSplitSelected) return;

        const splitOffset = playheadSeconds - selectedProductionFragment.timeline_start;
        const firstDuration = Number(splitOffset.toFixed(3));
        const secondDuration = Number((selectedProductionFragment.duration - splitOffset).toFixed(3));
        const splitSeed = Date.now();
        const fragmentPrefix = selectedProductionFragment.track_type === "music"
            ? "music_frag"
            : `${selectedProductionFragment.source_clip_id ?? "clip"}_frag`;

        const firstFragment: ProductionTimelineFragment = {
            ...selectedProductionFragment,
            id: `${fragmentPrefix}_${splitSeed}_a`,
            duration: firstDuration,
        };
        const secondFragment: ProductionTimelineFragment = {
            ...selectedProductionFragment,
            id: `${fragmentPrefix}_${splitSeed}_b`,
            source_start: Number((selectedProductionFragment.source_start + splitOffset).toFixed(3)),
            duration: secondDuration,
        };

        const nextTimeline = productionTimeline.flatMap((fragment) =>
            fragment.id === selectedProductionFragment.id
                ? [firstFragment, secondFragment]
                : [fragment]
        );

        updateProductionTimeline(nextTimeline, {
            selectedFragmentId: secondFragment.id,
            selectedTrack: selectedProductionTrack,
        });
    };

    const handleToggleSelectedProductionAudio = () => {
        if (displayStage !== 'production' || isBusy) return;
        if (selectedProductionTrack !== "audio") return;
        if (!selectedProductionFragment) return;

        const nextTimeline = productionTimeline.map((fragment) =>
            fragment.id === selectedProductionFragment.id
                ? { ...fragment, audio_enabled: !(fragment.audio_enabled ?? true) }
                : fragment
        );

        updateProductionTimeline(nextTimeline, {
            selectedFragmentId: selectedProductionFragment.id,
            selectedTrack: "audio",
        });
        window.setTimeout(() => {
            syncProductionMedia(playheadSeconds, isTimelinePlaying);
        }, 0);
    };

    const handleJumpToPreviousEdit = () => {
        const editPoints = Array.from(
            new Set([
                0,
                ...productionTimeline.flatMap((fragment) => [
                    Number(fragment.timeline_start.toFixed(3)),
                    Number((fragment.timeline_start + fragment.duration).toFixed(3)),
                ]),
            ])
        ).sort((left, right) => left - right);
        const previousPoint = [...editPoints].reverse().find((point) => point < playheadSeconds - 0.01) ?? 0;
        handleProductionSeek(previousPoint);
    };

    const handleJumpToNextEdit = () => {
        const editPoints = Array.from(
            new Set([
                ...productionTimeline.flatMap((fragment) => [
                    Number(fragment.timeline_start.toFixed(3)),
                    Number((fragment.timeline_start + fragment.duration).toFixed(3)),
                ]),
                Number(productionDuration.toFixed(3)),
            ])
        ).sort((left, right) => left - right);
        const nextPoint = editPoints.find((point) => point > playheadSeconds + 0.01) ?? productionDuration;
        handleProductionSeek(nextPoint);
    };

    const toggleTimelinePlayback = () => {
        if (!productionTimeline.length) return;
        if (isTimelinePlaying) {
            setIsTimelinePlaying(false);
            return;
        }
        if (playheadRef.current >= productionDuration) {
            handleProductionSeek(0);
        }
        setIsTimelinePlaying(true);
    };

    useEffect(() => {
        playheadRef.current = playheadSeconds;
    }, [playheadSeconds]);

    useEffect(() => {
        activeProductionMonitorSlotRef.current = activeProductionMonitorSlot;
    }, [activeProductionMonitorSlot]);

    useEffect(() => {
        productionMonitorSlotsRef.current = productionMonitorSlots;
    }, [productionMonitorSlots]);

    useEffect(() => {
        isProductionBufferingRef.current = isProductionBuffering;
    }, [isProductionBuffering]);

    useEffect(() => {
        if (!isProductionDisplay || !project) return;

        const activeSlot = activeProductionMonitorSlotRef.current;
        const inactiveSlot = getOtherProductionMonitorSlot(activeSlot);
        const currentSlotState = productionMonitorSlotsRef.current[activeSlot];
        const nextSlotState = productionMonitorSlotsRef.current[inactiveSlot];

        if (activeProductionFragment && activeProductionClip?.video_url) {
            if (
                currentSlotState.fragmentId !== activeProductionFragment.id
                && nextSlotState.fragmentId === activeProductionFragment.id
            ) {
                setActiveProductionMonitorSlotImmediate(inactiveSlot);
            } else if (currentSlotState.fragmentId !== activeProductionFragment.id) {
                primeProductionMonitorSlot(activeSlot, activeProductionFragment, activeProductionClip);
            }
        } else {
            primeProductionMonitorSlot(activeSlot, null, null);
        }

        const nextClip = nextProductionFragment
            ? project.timeline.find((clip) => clip.id === nextProductionFragment.source_clip_id) ?? null
            : null;
        primeProductionMonitorSlot(
            getOtherProductionMonitorSlot(activeProductionMonitorSlotRef.current),
            nextProductionFragment,
            nextClip,
        );
    }, [
        isProductionDisplay,
        project,
        activeProductionFragment?.id,
        activeProductionClip?.video_url,
        nextProductionFragment?.id,
    ]);

    useEffect(() => {
        if (!project?.music_url) {
            setProductionMusicDuration(0);
        }
    }, [project?.music_url]);

    useEffect(() => {
        if (!isProductionDisplay) {
            if (timelineAnimationFrameRef.current !== null) {
                cancelAnimationFrame(timelineAnimationFrameRef.current);
                timelineAnimationFrameRef.current = null;
            }
            productionVideoReadyCleanupRef.current?.();
            productionVideoReadyCleanupRef.current = null;
            timelineLastFrameRef.current = null;
            isProductionBufferingRef.current = false;
            setIsProductionBuffering(false);
            setIsTimelinePlaying(false);
            productionVideoARef.current?.pause();
            productionVideoBRef.current?.pause();
            productionMusicRef.current?.pause();
            updateProductionMonitorSlot("a", { fragmentId: null, src: null, sourceStart: 0 });
            updateProductionMonitorSlot("b", { fragmentId: null, src: null, sourceStart: 0 });
            setActiveProductionMonitorSlotImmediate("a");
        }
    }, [isProductionDisplay]);

    useEffect(() => {
        if (!isProductionDisplay) return;
        if (!productionTimeline.length) {
            setSelectedProductionFragmentId(null);
            setSelectedProductionTrack("video");
            setPlayheadSeconds(0);
            playheadRef.current = 0;
            return;
        }
        if (!selectedProductionFragmentId || !productionTimeline.some((fragment) => fragment.id === selectedProductionFragmentId)) {
            setSelectedProductionFragmentId(productionTimeline[0].id);
            setSelectedProductionTrack("video");
        }
        if (playheadRef.current > productionDuration) {
            handleProductionSeek(productionDuration);
        }
    }, [isProductionDisplay, productionTimeline, selectedProductionFragmentId, productionDuration]);

    useEffect(() => {
        if (!isProductionDisplay || isTimelinePlaying) return;
        syncProductionMedia(playheadSeconds, false);
    }, [isProductionDisplay, isTimelinePlaying, playheadSeconds]);

    useEffect(() => {
        if (!isProductionDisplay || !isTimelinePlaying) {
            productionVideoARef.current?.pause();
            productionVideoBRef.current?.pause();
            productionMusicRef.current?.pause();
            return;
        }
        syncProductionMedia(playheadSeconds, true);
    }, [isProductionDisplay, isTimelinePlaying, activeProductionFragment?.id, activeProductionClip?.video_url]);

    useEffect(() => {
        if (!isProductionDisplay || !isTimelinePlaying) return;

        const step = (timestamp: number) => {
            if (timelineLastFrameRef.current === null) {
                timelineLastFrameRef.current = timestamp;
            }

            if (isProductionBufferingRef.current) {
                timelineLastFrameRef.current = timestamp;
                timelineAnimationFrameRef.current = requestAnimationFrame(step);
                return;
            }

            const deltaSeconds = (timestamp - (timelineLastFrameRef.current ?? timestamp)) / 1000;
            timelineLastFrameRef.current = timestamp;
            const nextPlayhead = Math.min(productionDuration, playheadRef.current + deltaSeconds);

            playheadRef.current = nextPlayhead;
            setPlayheadSeconds(nextPlayhead);

            if (nextPlayhead >= productionDuration) {
                setIsTimelinePlaying(false);
                timelineAnimationFrameRef.current = null;
                timelineLastFrameRef.current = null;
                return;
            }

            timelineAnimationFrameRef.current = requestAnimationFrame(step);
        };

        timelineAnimationFrameRef.current = requestAnimationFrame(step);

        return () => {
            if (timelineAnimationFrameRef.current !== null) {
                cancelAnimationFrame(timelineAnimationFrameRef.current);
                timelineAnimationFrameRef.current = null;
            }
            timelineLastFrameRef.current = null;
        };
    }, [isProductionDisplay, isTimelinePlaying, productionDuration]);

    const handleFileUpload = async (event: React.ChangeEvent<HTMLInputElement>, type: string) => {
        const input = event.target;
        const file = input.files?.[0];
        input.value = '';
        if (!file || !project) return;

        let serverUrl: string;
        let serverName: string;
        let uploadedAsset: Awaited<ReturnType<typeof api.uploadAsset>>;
        try {
            uploadedAsset = await api.uploadAsset(project.project_id, file);
            serverUrl = uploadedAsset.url;
            serverName = uploadedAsset.name;
        } catch (e) {
            console.error("Upload failed", e);
            alert(e instanceof Error ? e.message : "Failed to upload file to backend.");
            return;
        }

        const resolvedAssetType = uploadedAsset.asset_type || type;
        const newAsset = {
            id: `asset_${Date.now()}`,
            url: serverUrl,
            type: resolvedAssetType,
            name: serverName,
            label: uploadedAsset.label ?? serverName,
            mime_type: uploadedAsset.mime_type ?? file.type,
            text_content: uploadedAsset.text_content ?? undefined,
            ai_context: uploadedAsset.ai_context ?? undefined,
        };

        const projectWithCurrentInputs = applyCurrentInputTextFields(project);
        const isAudioUpload = resolvedAssetType === 'audio';
        const nextAssets = isAudioUpload
            ? [...projectWithCurrentInputs.assets.filter((asset) => asset.type !== 'audio'), newAsset]
            : [...projectWithCurrentInputs.assets, newAsset];
        const nextMusicWorkflow = isAudioUpload
            ? (projectWithCurrentInputs.current_stage === 'input' ? 'uploaded_track' : 'lyria3')
            : projectWithCurrentInputs.music_workflow;

        const updatedProjectState = {
            ...projectWithCurrentInputs,
            assets: nextAssets,
            ...(isAudioUpload ? {
                music_url: serverUrl,
                music_workflow: nextMusicWorkflow,
                music_provider: selectedMusicProviderId,
                generated_music_provider: undefined,
                generated_music_lyrics_prompt: undefined,
                generated_music_style_prompt: undefined,
                generated_music_min_duration_seconds: undefined,
                generated_music_max_duration_seconds: undefined,
            } : {}),
        };

        try {
            const serverProject = await api.updateProject(project.project_id, updatedProjectState);
            setProject(serverProject);
        } catch (e) {
            console.error("Failed to sync new asset", e);
            alert("Failed to save asset to backend");
        }
    };

    const handleStoryboardFrameSwap = async (
        clipId: string,
        event: React.ChangeEvent<HTMLInputElement>
    ) => {
        const input = event.target;
        const file = input.files?.[0];
        input.value = '';
        if (!file || !project) return;

        const swapKey = `image:${clipId}`;
        try {
            setActiveMediaSwapKey(swapKey);
            const uploaded = await api.uploadAsset(project.project_id, file);
            const serverProject = await api.uploadStoryboardFrame(project.project_id, clipId, {
                url: uploaded.url,
                name: uploaded.name,
            });
            setProject(serverProject);
        } catch (e) {
            console.error("Storyboard frame swap failed", e);
            alert("Failed to replace storyboard frame.");
        } finally {
            setActiveMediaSwapKey(current => current === swapKey ? null : current);
        }
    };

    const handleFilmingClipSwap = async (
        clipId: string,
        event: React.ChangeEvent<HTMLInputElement>
    ) => {
        const input = event.target;
        const file = input.files?.[0];
        input.value = '';
        if (!file || !project) return;

        const swapKey = `video:${clipId}`;
        try {
            setActiveMediaSwapKey(swapKey);
            const uploaded = await api.uploadAsset(project.project_id, file);
            const updatedProjectState = {
                ...project,
                final_video_url: undefined,
                last_error: undefined,
                timeline: project.timeline.map(clip =>
                    clip.id === clipId
                        ? {
                            ...clip,
                            video_url: uploaded.url,
                            video_approved: true,
                            video_critiques: [
                                ...clip.video_critiques,
                                `Manual video clip uploaded: ${uploaded.name}`
                            ],
                        }
                        : clip
                ),
            };

            const serverProject = await api.updateProject(project.project_id, updatedProjectState);
            setProject(serverProject);
            setSelectedFilmingClipId(clipId);
        } catch (e) {
            console.error("Filming clip swap failed", e);
            alert("Failed to replace video clip.");
        } finally {
            setActiveMediaSwapKey(current => current === swapKey ? null : current);
        }
    };

    const removeAsset = async (assetId: string) => {
        if (!project) return;
        const projectWithCurrentInputs = applyCurrentInputTextFields(project);
        const newAssets = projectWithCurrentInputs.assets.filter(a => a.id !== assetId);

        // If we removed the audio track, clear music_url
        const assetToRemove = projectWithCurrentInputs.assets.find(a => a.id === assetId);
        const clearedMusicUrl = assetToRemove?.type === 'audio' ? undefined : projectWithCurrentInputs.music_url;
        const nextMusicWorkflow = assetToRemove?.type === 'audio' && projectWithCurrentInputs.music_workflow === 'uploaded_track'
            ? 'lyria3'
            : projectWithCurrentInputs.music_workflow;

        const updatedProjectState = {
            ...projectWithCurrentInputs,
            assets: newAssets,
            music_url: clearedMusicUrl,
            production_timeline: assetToRemove?.type === 'audio' ? [] : projectWithCurrentInputs.production_timeline,
            music_workflow: nextMusicWorkflow,
            ...(assetToRemove?.type === 'audio' ? {
                generated_music_provider: undefined,
                generated_music_lyrics_prompt: undefined,
                generated_music_style_prompt: undefined,
                generated_music_min_duration_seconds: undefined,
                generated_music_max_duration_seconds: undefined,
            } : {}),
        };

        try {
            const serverProject = await api.updateProject(project.project_id, updatedProjectState);
            setProject(serverProject);
        } catch (e) {
            console.error("Failed to remove asset", e);
        }
    };

    const handleClipEdit = (clipId: string, newText: string) => {
        if (!project) return;
        const newTimeline = project.timeline.map(clip =>
            clip.id === clipId ? { ...clip, storyboard_text: newText } : clip
        );
        setProject({ ...project, timeline: newTimeline });
    };

    const handleStoryboardTimelineChange = (nextTimeline: VideoClip[]) => {
        if (!project) return;
        const normalizedTimeline = recalcStoryboardTimeline(nextTimeline);
        const previousTimelineSignature = project.timeline.map((clip) => `${clip.id}:${quantizeStoryboardDuration(clip.duration)}`).join("|");
        const nextTimelineSignature = normalizedTimeline.map((clip) => `${clip.id}:${quantizeStoryboardDuration(clip.duration)}`).join("|");
        const timelineStructureChanged = previousTimelineSignature !== nextTimelineSignature;

        setProject({
            ...project,
            timeline: normalizedTimeline,
            production_timeline: timelineStructureChanged
                ? project.production_timeline.filter((fragment) => (fragment.track_type ?? "video") === "music")
                : project.production_timeline,
            final_video_url: timelineStructureChanged ? undefined : project.final_video_url,
            last_error: timelineStructureChanged ? undefined : project.last_error,
        });
    };

    const handleStoryboardDurationChange = (clipId: string, duration: number) => {
        if (!project) return;
        handleStoryboardTimelineChange(
            project.timeline.map((clip) =>
                clip.id === clipId
                    ? { ...clip, duration: quantizeStoryboardDuration(duration) }
                    : clip
            ),
        );
    };

    const handleStoryboardDeleteClip = (clipId: string) => {
        if (!project || project.timeline.length <= 1) return;
        const nextTimeline = project.timeline.filter((clip) => clip.id !== clipId);
        handleStoryboardTimelineChange(nextTimeline);
        if (selectedPlanningShotId === clipId) {
            setSelectedPlanningShotId(null);
        }
    };

    const handleStoryboardAddClip = () => {
        if (!project) return;
        const nextClip = createStoryboardClip();
        handleStoryboardTimelineChange([...project.timeline, nextClip]);
        setSelectedPlanningShotId(nextClip.id);
    };

    const handleStoryboardAiFill = async (clip: VideoClip, index: number) => {
        if (!project) return;
        setStoryboardAiFillClipId(clip.id);
        const apiKey = getStoredApiKey();
        const models = getStoredModels();
        const headers: Record<string, string> = {
            "Content-Type": "application/json",
            "X-Orchestrator-Model": models.orchestrator,
            "X-Text-Model": models.orchestrator,
        };
        if (apiKey) {
            headers["X-API-Key"] = apiKey;
        }

        try {
            const response = await fetch(`/api/projects/${project.project_id}/fill-clip`, {
                method: "POST",
                headers,
                body: JSON.stringify({
                    clip_id: clip.id,
                    clip_index: index,
                    total_clips: project.timeline.length,
                    surrounding_context: project.timeline
                        .filter((_, clipIndex) => Math.abs(clipIndex - index) <= 2 && clipIndex !== index)
                        .map((timelineClip) => timelineClip.storyboard_text)
                        .join(" | "),
                    duration: clip.duration,
                }),
            });
            if (!response.ok) {
                throw new Error("Fill failed");
            }
            const data = await response.json();
            handleClipEdit(clip.id, data.storyboard_text);
        } catch (error) {
            alert("AI fill-in failed. Try again or write it manually.");
        } finally {
            setStoryboardAiFillClipId(null);
        }
    };

    const handleStoryboardRegenerate = async (clipId: string) => {
        if (!project || storyboardRegeneratingClipId || isBusy) return;
        setStoryboardRegeneratingClipId(clipId);

        try {
            const serverProject = await api.regenerateStoryboardClip(project.project_id, clipId);
            setProject(serverProject);
            setViewedStage(null);
        } catch (error) {
            console.error("Storyboard regeneration failed", error);
            alert(error instanceof Error ? error.message : "Failed to regenerate storyboard frame.");
        } finally {
            setStoryboardRegeneratingClipId(null);
        }
    };

    const handleImageApproval = async (clipId: string, approved: boolean) => {
        if (!project) return;
        try {
            const serverProject = await api.updateStoryboardClipApproval(project.project_id, clipId, approved);
            setProject(serverProject);
        } catch (e) {
            console.error(e);
        }
    };

    const handleVideoApproval = async (clipId: string, approved: boolean) => {
        if (!project) return;
        const targetClip = project.timeline.find(clip => clip.id === clipId);
        if (approved && !targetClip?.video_url) return;
        try {
            const serverProject = await api.updateFilmingClipApproval(project.project_id, clipId, approved);
            setProject(serverProject);
        } catch (e) {
            console.error(e);
        }
    };

    const handleRunPipeline = async () => {
        if (!project || isBusy) return;
        if (project.current_stage === 'lyria_prompting' && usesManualMusicImport && !project.music_url) {
            alert("Import a rendered song before continuing to Planning.");
            return;
        }
        try {
            setIsRunning(true);
            let workingProject = project;

            if (workingProject.current_stage === 'input') {
                const screenplay = (document.getElementById('screenplay-input') as HTMLTextAreaElement)?.value || '';
                const instructions = (document.getElementById('instructions-input') as HTMLTextAreaElement)?.value || '';
                const lore = (document.getElementById('lore-input') as HTMLTextAreaElement)?.value || '';
                workingProject = {
                    ...workingProject,
                    screenplay,
                    instructions,
                    additional_lore: lore,
                    music_provider: selectedMusicProviderId,
                };
            } else if (
                workingProject.current_stage === 'lyria_prompting'
                || workingProject.current_stage === 'planning'
                || workingProject.current_stage === 'storyboarding'
                || workingProject.current_stage === 'filming'
                || workingProject.current_stage === 'production'
            ) {
                workingProject = {
                    ...workingProject,
                    music_provider: selectedMusicProviderId,
                };
            }

            if (
                workingProject.current_stage === 'input'
                || workingProject.current_stage === 'lyria_prompting'
                || workingProject.current_stage === 'planning'
                || workingProject.current_stage === 'storyboarding'
                || workingProject.current_stage === 'filming'
                || workingProject.current_stage === 'production'
            ) {
                workingProject = await api.updateProject(workingProject.project_id, workingProject);
                setProject(workingProject);
            }

            const isStoryboardingReviewOnly = (
                workingProject.current_stage === 'storyboarding'
                && !workingProject.timeline.every((clip) => clip.image_approved === true)
                && !workingProject.timeline.some((clip) => clip.image_approved === false)
            );
            const isFilmingReviewOnly = (
                workingProject.current_stage === 'filming'
                && !workingProject.timeline.every((clip) => clip.video_approved === true && !!clip.video_url)
                && !workingProject.timeline.some((clip) => clip.video_approved === false)
            );
            if (isStoryboardingReviewOnly || isFilmingReviewOnly) {
                setViewedStage(null);
                return;
            }

            const shouldUseAsyncStoryboardingRun = (
                workingProject.current_stage === 'planning'
                || (workingProject.current_stage === 'storyboarding' && !workingProject.timeline.every((clip) => clip.image_approved === true))
            );
            const shouldUseAsyncFilmingRun = (
                (workingProject.current_stage === 'storyboarding' && workingProject.timeline.every((clip) => clip.image_approved === true))
                || (workingProject.current_stage === 'filming' && workingProject.timeline.some((clip) => clip.video_approved === false))
            );

            let controller: AbortController | null = null;
            if (!shouldUseAsyncStoryboardingRun && !shouldUseAsyncFilmingRun) {
                controller = new AbortController();
                abortControllerRef.current = controller;
            }

            if (shouldUseAsyncStoryboardingRun || shouldUseAsyncFilmingRun) {
                const updatedProject = await api.runPipelineAsync(workingProject.project_id);
                setProject(updatedProject);
                setPipelineRunStatus({
                    is_running: true,
                    stage: shouldUseAsyncStoryboardingRun ? 'storyboarding' : 'filming',
                    started_at: new Date().toISOString(),
                });
                setViewedStage(null);
                if (shouldUseAsyncFilmingRun) {
                    setSelectedFilmingClipId(
                        updatedProject.timeline.find((clip) => clip.video_url)?.id
                        ?? updatedProject.timeline[0]?.id
                        ?? null
                    );
                }
                return;
            }

            const updatedProject = await api.runPipeline(workingProject.project_id, controller?.signal);
            setProject(updatedProject);
            setPipelineRunStatus({ is_running: false });
            setViewedStage(null);
        } catch (e: any) {
            if (e.name === 'AbortError' || e.message === 'Failed to run pipeline step') {
                console.log("Pipeline aborted or stopped");
            } else {
                console.error(e);
                alert("Pipeline execution failed");
            }
        } finally {
            setIsRunning(false);
            abortControllerRef.current = null;
        }
    };

    const handleAssetLabelChange = (assetId: string, nextLabel: string) => {
        if (!project) return;
        const projectWithCurrentInputs = applyCurrentInputTextFields(project);
        setProject({
            ...projectWithCurrentInputs,
            assets: projectWithCurrentInputs.assets.map((asset) =>
                asset.id === assetId ? { ...asset, label: nextLabel } : asset
            ),
        });
    };

    const handleAssetLabelCommit = async (assetId: string) => {
        if (!project) return;
        const projectWithCurrentInputs = applyCurrentInputTextFields(project);
        const nextProject = {
            ...projectWithCurrentInputs,
            assets: projectWithCurrentInputs.assets.map((asset) =>
                asset.id === assetId
                    ? { ...asset, label: asset.label?.trim() || null }
                    : asset
            ),
        };
        setProject(nextProject);

        try {
            const nextLabel = nextProject.assets.find((asset) => asset.id === assetId)?.label ?? null;
            const serverProject = await api.updateAssetLabel(nextProject.project_id, assetId, nextLabel);
            setProject(serverProject);
        } catch (e) {
            console.error("Failed to save asset label", e);
            alert("Failed to save asset label.");
        }
    };

    const handleRegenerateMusic = async () => {
        if (!project || isBusy || project.current_stage !== 'lyria_prompting') return;
        if (usesManualMusicImport) {
            alert("This project is using manual song import. Render the song in your external tool, then import the exported audio here.");
            return;
        }

        try {
            setIsRegeneratingMusic(true);
            musicStageAudioRef.current?.pause();
            setIsMusicStagePlaying(false);

            project.music_provider = selectedMusicProviderId;
            const persistedProject = await api.updateProject(project.project_id, project);
            const refreshedProject = await api.regenerateMusic(persistedProject.project_id);
            setProject(refreshedProject);
        } catch (e) {
            console.error("Music regeneration failed", e);
            alert(e instanceof Error ? e.message : "Failed to regenerate the song.");
        } finally {
            setIsRegeneratingMusic(false);
        }
    };

    const handleCopyMusicPromptPackage = async () => {
        if (!project) return;

        const prompt = buildMusicPromptPackageForClipboard(project);
        if (!prompt) {
            alert("Add lyrics or style notes first.");
            return;
        }

        try {
            await navigator.clipboard.writeText(prompt);
            alert("Copied the song prompt package to the clipboard.");
        } catch (error) {
            console.error("Failed to copy song prompt package", error);
            alert("Failed to copy the song prompt package.");
        }
    };

    const handleImportSong = () => {
        document.getElementById('music-song-upload')?.click();
    };

    const getExportFileName = (pathOrUrl: string | undefined, fallbackBase: string, defaultExtension: string) => {
        if (!pathOrUrl) return `${fallbackBase}${defaultExtension}`;
        try {
            const url = new URL(toBackendAssetUrl(pathOrUrl));
            const baseName = decodeURIComponent(url.pathname.split("/").filter(Boolean).pop() ?? "");
            const sanitized = baseName.replace(/[<>:"/\\|?*\u0000-\u001F]/g, "_");
            if (!sanitized) return `${fallbackBase}${defaultExtension}`;
            return sanitized.includes(".") ? sanitized : `${sanitized}${defaultExtension}`;
        } catch {
            return `${fallbackBase}${defaultExtension}`;
        }
    };

    const fetchExportBlob = async (pathOrUrl: string) => {
        const assetUrl = toBackendAssetUrl(pathOrUrl);
        if (shouldUseChunkedAssetFetch(pathOrUrl)) {
            return fetchAssetBlobInRanges(assetUrl);
        }

        const response = await fetch(assetUrl, { cache: "no-store" });
        if (!response.ok) {
            throw new Error(`Failed to download ${pathOrUrl} (${response.status})`);
        }
        return response.blob();
    };

    const triggerBlobDownload = (blob: Blob, fileName: string) => {
        const objectUrl = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = objectUrl;
        link.download = fileName;
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    };

    const loadFinalVideoBlob = async (pathOrUrl: string) => {
        const blob = await fetchExportBlob(pathOrUrl);
        finalVideoBlobRef.current = blob;
        return blob;
    };

    const handleExportFinalVideo = async () => {
        if (!project?.final_video_url || isExportingFinalVideo) return;

        try {
            setIsExportingFinalVideo(true);
            const blob = finalVideoBlobSource === project.final_video_url && finalVideoBlobRef.current
                ? finalVideoBlobRef.current
                : await loadFinalVideoBlob(project.final_video_url);
            triggerBlobDownload(
                blob,
                getExportFileName(project.final_video_url, `${project.project_id}_final`, ".mp4"),
            );
        } catch (error) {
            console.error("Failed to export final video", error);
            alert(error instanceof Error ? error.message : "Failed to export the final video.");
        } finally {
            setIsExportingFinalVideo(false);
        }
    };

    const writeBlobToHandle = async (directoryHandle: any, fileName: string, blob: Blob) => {
        const fileHandle = await directoryHandle.getFileHandle(fileName, { create: true });
        const writable = await fileHandle.createWritable();
        await writable.write(blob);
        await writable.close();
    };

    const handleExportResources = async () => {
        if (!project || !hasExportableResources || isExportingResources) return;

        const pickerWindow = window as Window & {
            showDirectoryPicker?: (options?: { mode?: "readwrite" }) => Promise<any>;
        };

        if (!pickerWindow.showDirectoryPicker) {
            alert("Folder export requires a Chromium-based browser that supports the File System Access API.");
            return;
        }

        try {
            setIsExportingResources(true);
            const rootDirectory = await pickerWindow.showDirectoryPicker({ mode: "readwrite" });
            let exportedCount = 0;

            if (project.music_url) {
                const musicDirectory = await rootDirectory.getDirectoryHandle("music", { create: true });
                const musicBlob = await fetchExportBlob(project.music_url);
                await writeBlobToHandle(
                    musicDirectory,
                    getExportFileName(project.music_url, `${project.project_id}_music`, ".mp3"),
                    musicBlob
                );
                exportedCount += 1;
            }

            const clipsToExport = project.timeline.filter((clip) => !!clip.video_url);
            if (clipsToExport.length > 0) {
                const clipsDirectory = await rootDirectory.getDirectoryHandle("clips", { create: true });
                for (const [index, clip] of clipsToExport.entries()) {
                    const clipBlob = await fetchExportBlob(clip.video_url!);
                    await writeBlobToHandle(
                        clipsDirectory,
                        getExportFileName(clip.video_url, `${String(index + 1).padStart(2, "0")}_${clip.id}`, ".mp4"),
                        clipBlob
                    );
                    exportedCount += 1;
                }
            }

            const framesToExport = project.timeline.filter((clip) => !!clip.image_url);
            if (framesToExport.length > 0) {
                const framesDirectory = await rootDirectory.getDirectoryHandle("frames", { create: true });
                for (const [index, clip] of framesToExport.entries()) {
                    const frameBlob = await fetchExportBlob(clip.image_url!);
                    await writeBlobToHandle(
                        framesDirectory,
                        getExportFileName(clip.image_url, `${String(index + 1).padStart(2, "0")}_${clip.id}`, ".png"),
                        frameBlob
                    );
                    exportedCount += 1;
                }
            }

            alert(`Exported ${exportedCount} project resource${exportedCount === 1 ? "" : "s"} to the selected folder.`);
        } catch (error) {
            if (error instanceof DOMException && error.name === "AbortError") {
                return;
            }
            console.error("Failed to export project resources", error);
            alert(error instanceof Error ? error.message : "Failed to export project resources.");
        } finally {
            setIsExportingResources(false);
        }
    };

    const getPreviousReviewStage = (stage: string): string => {
        if (stage === 'halted_for_review') {
            return getPreviousReviewStage(inferReviewStageForState(project));
        }
        if (stage === 'completed') {
            return stageNames[stageNames.length - 1] ?? 'input';
        }
        const stageIndex = stageNames.indexOf(stage);
        if (stageIndex <= 0) {
            return 'input';
        }
        return stageNames[stageIndex - 1] ?? 'input';
    };

    const REVERT_DESCRIPTION: Record<string, string> = {
        'input': 'everything (full reset) — your shots and music prompts will be cleared',
        'lyria_prompting': 'to the Music Prompts step — your lyrics and style will be preserved for editing',
        'planning': 'to the Planning step — the storyboard images will be cleared, your shot list is preserved',
        'storyboarding': 'to the Storyboarding step — the generated videos will be cleared, your images are preserved',
        'filming': 'to the Filming step — the final master video and production edit will be cleared, your clips are preserved',
        'production': 'to the Production step — the final render will be cleared, your edit timeline is preserved',
    };

    const handleRevert = async (targetStage: string) => {
        if (!project) return;

        // Wipe the confirmation dialog if open
        setConfirmDialog(null);

        // Abort any running pipeline request immediately
        if (abortControllerRef.current) {
            abortControllerRef.current.abort();
            abortControllerRef.current = null;
        }

        try {
            setIsRunning(true); // show loader during revert
            const reverted = await api.revert(project.project_id, targetStage);
            setProject(reverted);
            setPipelineRunStatus({ is_running: false });
            setViewedStage(null);
        } catch (e) {
            console.error(e);
            alert("Revert failed");
        } finally {
            setIsRunning(false);
        }
    };

    const handleSaveChanges = async () => {
        if (!project) return;
        try {
            setIsRunning(true);
            const nextProject = {
                ...applyCurrentInputTextFields(project),
                name: project.name.trim() || "Untitled Project",
                music_provider: selectedMusicProviderId,
            };
            const updated = await api.updateProject(nextProject.project_id, nextProject);
            setProject(updated);
            alert("Project saved.");
        } catch (e) {
            console.error("Save failed:", e);
            alert("Failed to save changes.");
        } finally {
            setIsRunning(false);
        }
    };

    const handleToggleAssetViewer = () => {
        setIsAssetViewerOpen((current) => {
            const next = !current;
            if (next && project) {
                setSelectedAssetId((previous) =>
                    previous && project.assets.some((asset) => asset.id === previous)
                        ? previous
                        : project.assets[0]?.id ?? null
                );
            }
            return next;
        });
    };

    const handleAssetViewerUploadRequest = (type: "audio" | "image" | "video" | "document") => {
        document.getElementById(`asset-viewer-${type}-upload`)?.click();
    };

    const handleLiveDirector = async (
        message: string,
        source: "text" | "voice",
        speechMode: "standard" | "realtime" = "standard",
    ): Promise<LiveDirectorResponse | null> => {
        if (!project || isBusy || isDirectorProcessing) return null;

        try {
            setIsDirectorProcessing(true);
            const response = await api.liveDirector(project.project_id, {
                message,
                display_stage: displayStage as ProjectState["current_stage"],
                selected_clip_id: (
                    displayStage === 'planning' || displayStage === 'storyboarding'
                        ? selectedPlanningShotId
                        : displayStage === 'filming'
                            ? selectedFilmingClipId
                            : null
                ) ?? undefined,
                selected_fragment_id: (
                    displayStage === 'production'
                        ? selectedProductionFragmentId
                        : null
                ) ?? undefined,
                selected_asset_id: (isAssetViewerOpen ? selectedAssetId : null) ?? undefined,
                source,
                speech_mode: speechMode,
            });

            setProject(response.project);
            if (
                response.project.active_run
                && (response.project.active_run.status === 'queued' || response.project.active_run.status === 'running')
            ) {
                setPipelineRunStatus({
                    is_running: true,
                    stage: response.project.active_run.stage,
                    started_at: response.project.active_run.started_at,
                    status: response.project.active_run.status,
                    driver: response.project.active_run.driver,
                });
            } else {
                setPipelineRunStatus({ is_running: false });
            }
            if (isAssetViewerOpen && response.navigation_action === 'stay') {
                setViewedStage(displayStage);
            } else {
                setViewedStage(null);
            }

            if (response.target_clip_id) {
                if (response.stage === 'planning' || response.stage === 'storyboarding') {
                    setSelectedPlanningShotId(response.target_clip_id);
                }
                if (response.stage === 'filming') {
                    setSelectedFilmingClipId(response.target_clip_id);
                }
            }

            if (response.target_fragment_id && response.stage === 'production') {
                setSelectedProductionFragmentId(response.target_fragment_id);
                setSelectedProductionTrack("audio");
            }
            if (isAssetViewerOpen) {
                const nextTargetAssetId = response.target_asset_id;
                if (nextTargetAssetId && response.project.assets.some((asset) => asset.id === nextTargetAssetId)) {
                    setSelectedAssetId(nextTargetAssetId);
                } else if (!response.project.assets.some((asset) => asset.id === selectedAssetId)) {
                    setSelectedAssetId(response.project.assets[0]?.id ?? null);
                }
            }
            return response;
        } catch (error) {
            console.error("Live Director Mode failed", error);
            alert(error instanceof Error ? error.message : "Live Director Mode failed.");
            return null;
        } finally {
            setIsDirectorProcessing(false);
        }
    };

    const handleRedo = async () => {
        if (!project || isBusy) return;

        setConfirmDialog({
            title: "Redo Stage",
            message: `Wipe all outputs for ${project.current_stage} and rerun?`,
            onConfirm: async () => {
                setConfirmDialog(null);
                try {
                    setIsRunning(true);
                    const newState = { ...project };

                    if (newState.current_stage === 'lyria_prompting') {
                        newState.lyrics_prompt = "";
                        newState.style_prompt = "";
                    } else if (newState.current_stage === 'storyboarding') {
                        newState.timeline = newState.timeline.map(clip => ({
                            ...clip,
                            image_url: undefined,
                            image_critiques: [],
                            image_approved: false
                        }));
                    } else if (newState.current_stage === 'filming') {
                        newState.timeline = newState.timeline.map(clip => ({
                            ...clip,
                            video_url: undefined,
                            video_critiques: [],
                            video_approved: false
                        }));
                    } else if (newState.current_stage === 'production') {
                        newState.production_timeline = buildDefaultProductionTimeline(newState.timeline, {
                            includeMusic: !!newState.music_url,
                            musicDuration: productionMusicDuration || undefined,
                            musicStartSeconds: normalizeMusicStartSeconds(newState.music_start_seconds),
                        });
                        newState.final_video_url = undefined;
                    }

                    const updated = await api.updateProject(project.project_id, newState);
                    setProject(updated);
                    setViewedStage(null);
                } catch (e) {
                    console.error(e);
                    alert("Redo reset failed");
                } finally {
                    setIsRunning(false);
                }
            }
        });
    };

    if (loadError) {
        return (
            <div className="min-h-dvh w-full flex items-center justify-center bg-background p-6">
                <GlassCard className="max-w-xl w-full !p-6">
                    <div className="flex items-start gap-4">
                        <div className="w-10 h-10 rounded-full bg-rose-500/15 text-rose-400 flex items-center justify-center shrink-0">
                            <AlertCircle className="w-5 h-5" />
                        </div>
                        <div className="space-y-3">
                            <div>
                                <h1 className="text-lg font-semibold text-white">Backend Unavailable</h1>
                                <p className="text-sm text-surface-border mt-1">{loadError}</p>
                            </div>
                            <p className="text-xs text-surface-border">
                                Start the backend, then refresh this page. If you use `start.bat`, it now launches the API with `python -m uvicorn`.
                            </p>
                        </div>
                    </div>
                </GlassCard>
            </div>
        );
    }

    if (!project) {
        return <div className="min-h-dvh w-full flex items-center justify-center bg-background"><Loader2 className="w-12 h-12 text-primary animate-spin" /></div>;
    }

    return (
        <div className="h-dvh w-full flex flex-col overflow-hidden bg-background">
            {project.music_url && displayStage !== 'production' && (
                <audio
                    key={`timeline-music-metadata:${project.music_url}`}
                    src={toBackendAssetUrl(project.music_url)}
                    preload="metadata"
                    className="hidden"
                    onLoadedMetadata={(event) => {
                        const duration = Number.isFinite(event.currentTarget.duration)
                            ? event.currentTarget.duration
                            : 0;
                        setProductionMusicDuration(duration);
                    }}
                    onDurationChange={(event) => {
                        const duration = Number.isFinite(event.currentTarget.duration)
                            ? event.currentTarget.duration
                            : 0;
                        setProductionMusicDuration(duration);
                    }}
                />
            )}

            {/* Top Navigation / Toolbar */}
            <header className="h-16 border-b border-surface-border glass flex items-center justify-between px-6 z-20">
                <div className="flex items-center space-x-4">
                    <div className="w-8 h-8 rounded bg-gradient-to-br from-primary to-purple-600 flex items-center justify-center">
                        <Video className="w-4 h-4 text-white" />
                    </div>
                    <div className="min-w-0">
                        <label className="sr-only" htmlFor="project-name-input">Project name</label>
                        <input
                            id="project-name-input"
                            value={project.name}
                            onChange={(e) => setProject({ ...project, name: e.target.value })}
                            className="w-[18rem] max-w-full bg-transparent border border-transparent focus:border-primary/40 rounded-lg px-3 py-1.5 text-lg font-semibold text-white outline-none transition-colors"
                            placeholder="Project name"
                        />
                        <p className="text-xs text-surface-border px-3 truncate">{projectId}</p>
                    </div>
                </div>

                {/* Progress Stepper */}
                <div className="hidden md:flex items-center space-x-2">
                    {stages.map((stage, idx) => {
                        const isClickable = idx <= activeStageIndex;
                        const isViewed = displayStageIndex === idx;
                        return (
                            <div key={stage} className="flex items-center">
                                <button
                                    onClick={() => isClickable && setViewedStage(stageNames[idx])}
                                    disabled={!isClickable}
                                    aria-label={`View ${stage} stage`}
                                    className={`px-3 py-1 rounded-full text-sm font-medium transition-colors ${isViewed ? "bg-primary text-white cursor-default" :
                                        idx <= activeStageIndex ? "bg-surface-border text-white/70 hover:bg-surface-hover hover:text-white cursor-pointer" :
                                            "text-surface-border cursor-not-allowed opacity-50"
                                        }`}>
                                    {idx + 1}. {stage}
                                </button>
                                {idx < stages.length - 1 && (
                                    <div className={`w-8 h-[2px] mx-2 ${activeStageIndex > idx ? "bg-primary" : "bg-surface-border"}`} />
                                )}
                            </div>
                        );
                    })}
                </div>

                <div className="flex items-center space-x-4">
                    <button
                        title="Save Project"
                        onClick={handleSaveChanges}
                        disabled={isBusy}
                        className={`inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium transition-colors ${isBusy ? "border-surface-border text-white/30 cursor-not-allowed" : "border-emerald-500/30 text-emerald-300 hover:bg-emerald-500/15"}`}
                    >
                        <Save className="w-4 h-4" />
                        Save Project
                    </button>
                    <button
                        title={isAssetViewerOpen ? "Close Asset Viewer" : "Open Asset Viewer"}
                        onClick={handleToggleAssetViewer}
                        className={`inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium transition-colors ${isAssetViewerOpen
                            ? "border-cyan-500/35 bg-cyan-500/15 text-cyan-200 hover:bg-cyan-500/20"
                            : "border-surface-border text-white/75 hover:bg-surface-hover"
                            }`}
                    >
                        <ImageIcon className="w-4 h-4" />
                        {isAssetViewerOpen ? "Close Assets" : "Asset Viewer"}
                    </button>
                    <button
                        title="Back to Projects"
                        onClick={() => router.push("/")}
                        className="p-2 rounded hover:bg-surface-hover text-surface-border hover:text-white transition-colors"
                    >
                        <Home className="w-5 h-5" />
                    </button>
                    <button
                        title="Settings"
                        onClick={() => setIsSettingsOpen(true)}
                        className="p-2 rounded hover:bg-surface-hover text-surface-border hover:text-white transition-colors"
                    >
                        <Settings className="w-5 h-5" />
                    </button>
                </div>
            </header>

            {/* Main Workspace Area */}
            <main className="flex-1 min-h-0 flex overflow-hidden relative">
                <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-surface/20 via-background to-background" />
                {isAssetViewerOpen ? (
                    <AssetViewerWorkspace
                        assets={project.assets}
                        selectedAssetId={selectedAssetId}
                        isBusy={isBusy}
                        onSelectAsset={setSelectedAssetId}
                        onLabelChange={handleAssetLabelChange}
                        onLabelCommit={handleAssetLabelCommit}
                        onRemoveAsset={(assetId) => { void removeAsset(assetId); }}
                        onRequestUpload={handleAssetViewerUploadRequest}
                    />
                ) : (
                    <React.Fragment>
                {/* Left pane: Agent Actions & Storyboard Output */}
                <section className={`${(displayStage === 'input' || displayStage === 'storyboarding') ? 'w-full' : 'w-1/3 min-w-[300px]'} border-r border-surface-border glass m-4 rounded-xl flex min-h-0 flex-col z-10 overflow-hidden`}>
                    {/* This area will host the forms, agent chat, and clip settings */}
                    <div className="p-4 border-b border-surface-border glass sticky top-0 z-10 bg-surface/80 backdrop-blur-md">
                        <h2 className="font-semibold text-white/90">Agentic Workspace</h2>
                        <p className="text-sm text-surface-border">
                            {displayStage === 'input' && "Configure inputs and prime Gemini 3.1 Pro."}
                            {displayStage === 'lyria_prompting' && `Review and refine the drafted lyrics and musical direction for ${selectedMusicProvider.label}, then generate the song when you are ready.`}
                            {displayStage === 'planning' && "Review Gemini's timeline chunks before NanoBanana storyboarding."}
                            {displayStage === 'storyboarding' && (
                                isStoryboardingRunActive
                                    ? "Watch NanoBanana initial frames arrive live as each shot finishes."
                                    : "Review NanoBanana 2 initial frames."
                            )}
                            {displayStage === 'filming' && (isFilmingRunActive ? "Watch Veo 3.1 renders arrive live as each shot finishes." : "Review Veo 3.1 video renders.")}
                            {displayStage === 'production' && "Trim, split, and reorder the final edit before rendering."}
                            {displayStage === 'completed' && "Review the rendered master and optionally return to Production for more edits."}
                            {displayStage === 'halted_for_review' && "Pipeline halted due to an error."}
                        </p>
                    </div>
                    <div className="flex-1 p-4 overflow-y-auto space-y-4">
                        {stageSummary && (
                            <GlassCard className="!p-4 space-y-3 border border-cyan-500/20">
                                {stageVoiceBriefsEnabled && stageSummary.audio_url && (
                                    <audio
                                        key={`${displayStage}-${stageSummary.generated_at}`}
                                        ref={stageBriefAudioRef}
                                        preload="auto"
                                        className="hidden"
                                        src={toBackendAssetUrl(stageSummary.audio_url)}
                                    />
                                )}
                                <div className="flex items-start justify-between gap-3">
                                    <div>
                                        <h3 className="text-sm font-semibold text-white/90 flex items-center gap-2">
                                            <Music className="w-4 h-4 text-cyan-300" />
                                            Orchestrator Brief
                                        </h3>
                                        <p className="text-xs text-surface-border mt-1">
                                            A short spoken review of this stage, including what looks strong and what may need your attention.
                                        </p>
                                    </div>
                                    <span className="text-[10px] uppercase tracking-[0.18em] text-cyan-300/80">
                                        {displayStage}
                                    </span>
                                </div>
                                {stageVoiceBriefsEnabled && stageSummary.audio_url ? (
                                    <div className="rounded-lg border border-cyan-500/20 bg-cyan-500/5 px-3 py-2 text-xs text-cyan-100/80">
                                        {displayStage === project.current_stage
                                            ? "The spoken brief plays automatically in the background when this stage becomes ready."
                                            : "The spoken brief is only auto-played on the active stage. The transcript remains available here."}
                                    </div>
                                ) : (
                                    <div className="rounded-lg border border-dashed border-surface-border bg-background/30 px-3 py-2 text-xs text-surface-border">
                                        {stageVoiceBriefsEnabled
                                            ? "Audio playback is unavailable for this brief, but the transcript is still below."
                                            : "Voice briefs are disabled in Settings, so only the transcript is shown here."}
                                    </div>
                                )}
                                <div className="rounded-lg border border-surface-border bg-background/40 p-3 text-sm leading-relaxed text-white/85">
                                    {stageSummary.text}
                                </div>
                            </GlassCard>
                        )}
                        {displayStage === 'input' && (
                            <>
                                <GlassCard>
                                    <h3 className="text-sm font-medium mb-3 text-white/80 flex items-center">
                                        <ListPlus className="w-4 h-4 mr-2 text-primary" />
                                        Context & Instructions
                                    </h3>
                                    <div className="space-y-4">
                                        <div>
                                            <label className="text-xs text-surface-border font-medium mb-1 block">Screenplay / Story</label>
                                            <textarea
                                                id="screenplay-input"
                                                defaultValue={project.screenplay}
                                                className="w-full h-24 bg-background border border-surface-border rounded-lg p-3 text-sm focus:outline-none focus:border-primary/50 resize-none"
                                                placeholder="Describe the scenes for your music video..."
                                            />
                                        </div>
                                        <div className="grid grid-cols-2 gap-3">
                                            <div>
                                                <label className="text-xs text-surface-border font-medium mb-1 block">General Instructions</label>
                                                <textarea
                                                    id="instructions-input"
                                                    defaultValue={project.instructions}
                                                    className="w-full h-24 bg-background border border-surface-border rounded-lg p-3 text-sm focus:outline-none focus:border-primary/50 resize-none"
                                                    placeholder="E.g., Dark mood, cinematic lighting..."
                                                />
                                            </div>
                                            <div>
                                                <label className="text-xs text-surface-border font-medium mb-1 block">Additional Lore</label>
                                                <textarea
                                                    id="lore-input"
                                                    defaultValue={project.additional_lore}
                                                    className="w-full h-24 bg-background border border-surface-border rounded-lg p-3 text-sm focus:outline-none focus:border-primary/50 resize-none"
                                                    placeholder="Background details for the agent..."
                                                />
                                                <p className="mt-2 text-[11px] text-surface-border">
                                                    Upload PDF or DOCX references below if you want to give the agent richer story or world context without cramming everything into lore.
                                                </p>
                                            </div>
                                        </div>
                                    </div>

                                    {/* Assets */}
                                    <div className="mt-6 pt-4 border-t border-surface-border">
                                        <h3 className="text-sm font-medium mb-3 text-white/80 flex items-center">
                                            <ImageIcon className="w-4 h-4 mr-2 text-primary" />
                                            Uploaded Assets
                                        </h3>
                                        <p className="mb-4 text-xs text-surface-border">
                                            Give each asset a meaningful label like a character, prop, or location name. Gemini uses those labels as semantic references across planning and storyboarding.
                                        </p>
                                        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                                            <input
                                                type="file"
                                                id="audio-upload"
                                                title="Upload Audio Track"
                                                className="hidden"
                                                accept="audio/*"
                                                onChange={(e) => handleFileUpload(e, 'audio')}
                                            />
                                            <div
                                                onClick={() => document.getElementById('audio-upload')?.click()}
                                                className="relative overflow-hidden rounded-2xl border border-dashed border-surface-border bg-background/50 hover:bg-surface-hover/50 transition-colors cursor-pointer group"
                                            >
                                                <div className="aspect-video flex flex-col items-center justify-center text-center p-4">
                                                    <div className="w-10 h-10 rounded-full bg-surface-border flex items-center justify-center mb-2 group-hover:bg-primary/20 group-hover:text-primary transition-colors text-white/50">
                                                        <Music className="w-5 h-5" />
                                                    </div>
                                                    <span className="text-sm font-medium text-white/90">Add Audio</span>
                                                    <span className="text-xs text-surface-border mt-1">Song, stem, or music reference</span>
                                                </div>
                                            </div>

                                            <input
                                                type="file"
                                                id="image-upload"
                                                title="Upload Image Asset"
                                                className="hidden"
                                                accept="image/*"
                                                onChange={(e) => handleFileUpload(e, 'image')}
                                            />
                                            <div
                                                onClick={() => document.getElementById('image-upload')?.click()}
                                                className="relative overflow-hidden rounded-2xl border border-dashed border-surface-border bg-background/50 hover:bg-surface-hover/50 transition-colors cursor-pointer group"
                                            >
                                                <div className="aspect-video flex flex-col items-center justify-center text-center p-4">
                                                    <div className="w-10 h-10 rounded-full bg-surface-border flex items-center justify-center mb-2 group-hover:bg-primary/20 group-hover:text-primary transition-colors text-white/50">
                                                        <ImageIcon className="w-5 h-5" />
                                                    </div>
                                                    <span className="text-sm font-medium text-white/90">Add Image</span>
                                                    <span className="text-xs text-surface-border mt-1">Character, prop, vehicle, or location reference</span>
                                                </div>
                                            </div>

                                            <input
                                                type="file"
                                                id="document-upload"
                                                title="Upload Context Document"
                                                className="hidden"
                                                accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                                                onChange={(e) => handleFileUpload(e, 'document')}
                                            />
                                            <div
                                                onClick={() => document.getElementById('document-upload')?.click()}
                                                className="relative overflow-hidden rounded-2xl border border-dashed border-surface-border bg-background/50 hover:bg-surface-hover/50 transition-colors cursor-pointer group"
                                            >
                                                <div className="aspect-video flex flex-col items-center justify-center text-center p-4">
                                                    <div className="w-10 h-10 rounded-full bg-surface-border flex items-center justify-center mb-2 group-hover:bg-primary/20 group-hover:text-primary transition-colors text-white/50">
                                                        <FileText className="w-5 h-5" />
                                                    </div>
                                                    <span className="text-sm font-medium text-white/90">Add Document</span>
                                                    <span className="text-xs text-surface-border mt-1">PDF or DOCX story and world reference</span>
                                                </div>
                                            </div>

                                            {project.assets.map(asset => (
                                                <div key={asset.id} className="relative overflow-hidden rounded-2xl border border-surface-border bg-surface/40 backdrop-blur group">
                                                    <div className="relative aspect-video overflow-hidden border-b border-surface-border bg-background/70">
                                                        {asset.type === 'image' && (
                                                            <img
                                                                src={toBackendAssetUrl(asset.url)}
                                                                alt={asset.label?.trim() || asset.name}
                                                                className="absolute inset-0 w-full h-full object-cover"
                                                            />
                                                        )}
                                                        {asset.type === 'audio' && (
                                                            <div className="absolute inset-0 flex flex-col items-center justify-center text-center p-4 bg-gradient-to-br from-emerald-500/10 via-background to-background">
                                                                <Music className="w-12 h-12 text-emerald-300/60 mb-2" />
                                                                <span className="text-xs uppercase tracking-[0.22em] text-emerald-200/70">Audio</span>
                                                            </div>
                                                        )}
                                                        {asset.type === 'document' && (
                                                            <div className="absolute inset-0 p-4 flex flex-col justify-between bg-gradient-to-br from-slate-900/90 via-slate-900/70 to-slate-800/50">
                                                                <FileText className="w-10 h-10 text-white/25" />
                                                                <p className="text-[11px] leading-relaxed text-white/60 line-clamp-4">
                                                                    {asset.ai_context || asset.text_content || "Document uploaded for contextual reference."}
                                                                </p>
                                                            </div>
                                                        )}
                                                        <div className="absolute left-3 top-3 rounded-full border border-white/10 bg-black/45 px-2 py-1 text-[10px] uppercase tracking-[0.18em] text-white/75 backdrop-blur-sm">
                                                            {asset.type}
                                                        </div>
                                                        <button
                                                            onClick={(e) => { e.stopPropagation(); removeAsset(asset.id); }}
                                                            className="absolute top-3 right-3 w-7 h-7 rounded-full bg-red-500/80 text-white flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity hover:bg-red-500 backdrop-blur-sm"
                                                        >
                                                            <X className="w-4 h-4" />
                                                        </button>
                                                    </div>
                                                    <div className="p-3 space-y-2">
                                                        <div>
                                                            <label className="text-[11px] uppercase tracking-[0.18em] text-surface-border block mb-1">
                                                                AI Label
                                                            </label>
                                                            <input
                                                                value={asset.label ?? ""}
                                                                onChange={(e) => handleAssetLabelChange(asset.id, e.target.value)}
                                                                onBlur={() => handleAssetLabelCommit(asset.id)}
                                                                onKeyDown={(e) => {
                                                                    if (e.key === "Enter") {
                                                                        e.preventDefault();
                                                                        (e.currentTarget as HTMLInputElement).blur();
                                                                    }
                                                                }}
                                                                placeholder="e.g. Mira, Neon Alley, Red Motorcycle"
                                                                className="w-full rounded-lg border border-surface-border bg-background/60 px-3 py-2 text-sm text-white/90 focus:outline-none focus:border-primary/50"
                                                            />
                                                        </div>
                                                        <div className="text-xs text-surface-border">
                                                            <div className="truncate">{asset.name}</div>
                                                            {asset.type === 'document' && (
                                                                <p className="mt-1 text-[11px] leading-relaxed text-white/55 line-clamp-2">
                                                                    {asset.ai_context || asset.text_content || "Supplemental document context"}
                                                                </p>
                                                            )}
                                                            {asset.type === 'image' && (
                                                                <p className="mt-1 text-[11px] leading-relaxed text-white/55">
                                                                    Use labels for named characters, props, creatures, vehicles, or locations so the agent can route this reference automatically.
                                                                </p>
                                                            )}
                                                            {asset.ai_context && asset.type !== 'document' && (
                                                                <p className="mt-2 rounded-lg border border-white/10 bg-background/50 px-3 py-2 text-[11px] leading-relaxed text-white/65 line-clamp-4">
                                                                    {asset.ai_context}
                                                                </p>
                                                            )}
                                                        </div>
                                                    </div>
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                </GlassCard>
                            </>
                        )}

                        {displayStage === 'lyria_prompting' && (
                            <div className="space-y-4 mb-6">
                                <div className="p-3 bg-surface-hover/30 border border-primary/20 rounded-xl">
                                    <p className="text-sm text-white/80 font-medium">
                                        {usesManualMusicImport
                                            ? "Gemini has drafted your external song lyrics and style."
                                            : "Gemini has drafted your lyrics and musical direction."}
                                    </p>
                                    <p className="text-xs text-surface-border mt-1">
                                        {usesManualMusicImport
                                            ? "Edit the lyrics and style below, render the song in your external tool, import the exported audio here, then continue to Planning."
                                            : "Edit the lyrics, style, and song length first. Music is only generated when you press `Generate Song`, and Planning stays locked until that generated song matches your current settings."}
                                    </p>
                                </div>
                                {!usesManualMusicImport && !selectedMusicProvider.usesLyrics && (
                                    <div className="p-3 bg-amber-500/10 border border-amber-500/30 rounded-xl">
                                        <p className="text-sm text-amber-200 font-medium">
                                            Lyrics will not be used with {selectedMusicProvider.label}.
                                        </p>
                                        <p className="text-xs text-amber-100/80 mt-1">
                                            This provider is instrumental-only. Keep the lyrics if you want them saved in the project, but only style and song length affect the generated result.
                                        </p>
                                    </div>
                                )}
                                {project.last_error && (
                                    <div className="p-3 bg-rose-500/10 border border-rose-500/30 rounded-xl">
                                        <p className="text-sm text-rose-300 font-medium">
                                            {usesManualMusicImport ? "Song import needs attention." : "Music generation needs attention."}
                                        </p>
                                        <p className="text-xs text-rose-200/80 mt-1 break-words">{project.last_error}</p>
                                    </div>
                                )}
                                <GlassCard className="!p-4">
                                    <input
                                        type="file"
                                        id="music-song-upload"
                                        title="Import song"
                                        className="hidden"
                                        accept="audio/*"
                                        onChange={(e) => handleFileUpload(e, 'audio')}
                                    />
                                    <div className="flex items-start justify-between gap-3">
                                        <div>
                                            <h3 className="text-xs font-semibold text-primary uppercase tracking-wider flex items-center">
                                                <Music className="w-3.5 h-3.5 mr-1.5" />
                                                {usesManualMusicImport ? "Imported Song" : "Current Generated Song"}
                                            </h3>
                                            <p className="text-xs text-surface-border mt-1">
                                                {usesManualMusicImport
                                                    ? (project.music_url
                                                        ? "Use the footer transport to audition the imported song before moving on."
                                                        : "No imported song is attached yet. Render it in your external tool, then import the exported audio here.")
                                                    : hasCurrentAutomaticMusicTrack
                                                        ? "Use the footer transport to audition the current full-quality song before moving on."
                                                        : musicTrackNeedsRegeneration
                                                            ? "The attached song is out of date for the current music settings. Generate it again before continuing to Planning."
                                                            : "No generated song is attached yet. Click `Generate Song` after you finish tweaking the prompts."}
                                            </p>
                                        </div>
                                        <div className="flex flex-wrap justify-end gap-2">
                                            {usesManualMusicImport && (
                                                <button
                                                    onClick={handleImportSong}
                                                    disabled={displayStage !== project.current_stage || isBusy}
                                                    className={`inline-flex items-center justify-center rounded-lg border px-3 py-2 text-xs font-medium transition-colors ${displayStage === project.current_stage && !isBusy
                                                        ? "border-cyan-500/30 bg-cyan-500/10 text-cyan-200 hover:bg-cyan-500/20"
                                                        : "border-surface-border bg-background/40 text-white/30 cursor-not-allowed"
                                                        }`}
                                                >
                                                    {project.music_url ? "Replace Imported Song" : "Import Song"}
                                                </button>
                                            )}
                                            {project.music_url && (
                                                <a
                                                    href={toBackendAssetUrl(project.music_url)}
                                                    download
                                                    target="_blank"
                                                    rel="noreferrer"
                                                    className="inline-flex items-center justify-center rounded-lg border border-cyan-500/30 bg-cyan-500/10 px-3 py-2 text-xs font-medium text-cyan-200 transition-colors hover:bg-cyan-500/20"
                                                >
                                                    Download Song
                                                </a>
                                            )}
                                        </div>
                                    </div>
                                            {usesManualMusicImport && (
                                                <div className="mt-4 rounded-xl border border-surface-border bg-background/30 p-3">
                                                    <div className="flex items-center justify-between gap-3">
                                                        <div>
                                                            <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-white/65">Prompt Package</p>
                                                    <p className="text-xs text-surface-border mt-1">Copy this package into your external tool, render the song, then import the exported audio file back into this project.</p>
                                                </div>
                                                <button
                                                    onClick={() => void handleCopyMusicPromptPackage()}
                                                    className="inline-flex items-center justify-center rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs font-medium text-amber-200 transition-colors hover:bg-amber-500/20"
                                                >
                                                    Copy Prompt Package
                                                </button>
                                            </div>
                                        </div>
                                    )}
                                </GlassCard>
                                <GlassCard className="!p-4">
                                    <h3 className="text-xs font-semibold text-primary uppercase tracking-wider mb-2 flex items-center">
                                        <Music className="w-3.5 h-3.5 mr-1.5" />
                                        Song Length Range
                                    </h3>
                                    <p className="text-xs text-surface-border mb-3">
                                        These bounds control the actual song length target, not just playback in the Music stage. Keep your imported or generated song inside this range before Planning measures it and builds the shot timeline.
                                    </p>
                                    <div className="grid grid-cols-2 gap-3">
                                        <label className="flex flex-col gap-1.5">
                                            <span className="text-[11px] font-medium uppercase tracking-[0.18em] text-white/60">Min Seconds</span>
                                            <input
                                                type="number"
                                                min={8}
                                                step={1}
                                                value={project.music_min_duration_seconds ?? DEFAULT_MUSIC_MIN_DURATION_SECONDS}
                                                onChange={(e) => handleMusicDurationBoundChange("music_min_duration_seconds", e.target.value)}
                                                className="w-full bg-background/50 border border-surface-border rounded p-2 text-sm text-white/90 focus:outline-none focus:border-primary/50"
                                            />
                                        </label>
                                        <label className="flex flex-col gap-1.5">
                                            <span className="text-[11px] font-medium uppercase tracking-[0.18em] text-white/60">Max Seconds</span>
                                            <input
                                                type="number"
                                                min={8}
                                                step={1}
                                                value={project.music_max_duration_seconds ?? DEFAULT_MUSIC_MAX_DURATION_SECONDS}
                                                onChange={(e) => handleMusicDurationBoundChange("music_max_duration_seconds", e.target.value)}
                                                className="w-full bg-background/50 border border-surface-border rounded p-2 text-sm text-white/90 focus:outline-none focus:border-primary/50"
                                            />
                                        </label>
                                    </div>
                                    <div className="mt-3 rounded-lg border border-surface-border bg-background/40 px-3 py-2 text-xs text-surface-border">
                                        Active generation range: {musicDurationBounds.min.toFixed(0)}s to {musicDurationBounds.max.toFixed(0)}s.
                                    </div>
                                </GlassCard>
                                <GlassCard className="!p-4">
                                    <h3 className="text-xs font-semibold text-primary uppercase tracking-wider mb-2">Musical Style</h3>
                                    <textarea
                                        value={project.style_prompt ?? ''}
                                        onChange={(e) => setProject({ ...project, style_prompt: e.target.value })}
                                        className="w-full min-h-[60px] bg-background/50 border border-surface-border rounded p-2 text-sm text-white/90 focus:outline-none focus:border-primary/50 resize-y"
                                        placeholder="e.g. Cinematic, orchestral, melancholic, female vocals..."
                                    />
                                </GlassCard>
                            </div>
                        )}

                        {displayStage === 'planning' && (
                            <div className="space-y-4 mb-6">
                                    <div className="p-3 bg-surface-hover/30 border border-primary/20 rounded-xl mb-4">
                                        <p className="text-sm text-white/80 font-medium">Gemini Draft Generation Complete.</p>
                                    <p className="text-xs text-surface-border mt-1">Review and edit the shots below. Drag to reorder, tweak durations in 4s / 6s / 8s increments, add new shots, or click ✨ AI Fill to let Gemini write one. Changes are saved when you click Generate Storyboards.</p>
                                </div>
                                <GlassCard className="!p-4 space-y-4">
                                    <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
                                        <div>
                                            <h3 className="text-xs font-semibold text-primary uppercase tracking-wider">Music Start</h3>
                                            <p className="mt-1 text-xs text-surface-border">
                                                Set where the song begins relative to the visual plan. This lets you intentionally leave room for pre-music intro shots and post-music ending shots.
                                            </p>
                                        </div>
                                        <label className="flex flex-col gap-1.5 lg:min-w-[12rem]">
                                            <span className="text-[11px] font-medium uppercase tracking-[0.18em] text-white/60">Music Start (Seconds)</span>
                                            <input
                                                type="number"
                                                min={0}
                                                step={0.5}
                                                value={planningMusicStartSeconds}
                                                onChange={(e) => handlePlanningMusicStartChange(e.target.value)}
                                                className="w-full rounded-lg border border-surface-border bg-background/50 p-2 text-sm text-white/90 focus:outline-none focus:border-primary/50"
                                            />
                                        </label>
                                    </div>
                                </GlassCard>
                                <ShotListEditor
                                    clips={project.timeline}
                                    projectId={project.project_id}
                                    expandedShotId={selectedPlanningShotId}
                                    onChange={(clips) => setProject({ ...project, timeline: clips })}
                                    onExpand={(id) => setSelectedPlanningShotId(prev => prev === id ? null : id)}
                                />
                            </div>
                        )}

                        {displayStage === 'halted_for_review' && (
                            <div className="space-y-4 mb-6">
                                <div className="p-4 bg-rose-500/10 border border-rose-500/30 rounded-xl mb-4">
                                    <h3 className="text-lg text-rose-400 font-semibold mb-2">Pipeline Halted</h3>
                                    <p className="text-sm text-white/80">The processing pipeline encountered a critical error or was deliberately halted for review.</p>
                                    <p className="text-sm text-rose-300 mt-2 font-mono p-2 bg-black/30 rounded-lg">{project.last_error || "Unknown Error"}</p>
                                    <p className="text-xs text-surface-border mt-4">You can attempt to retry the current stage, or stop and back up to the previous stage.</p>
                                </div>
                            </div>
                        )}

                        {displayStage === 'storyboarding' && (
                            <div className="space-y-4 mb-6">
                                <div className="p-3 bg-surface-hover/30 border border-primary/20 rounded-xl mb-4">
                                    <p className="text-sm text-white/80 font-medium">
                                        {isStoryboardingRunActive ? "NanoBanana is generating initial frames." : "NanoBanana Initial Frames Generated."}
                                    </p>
                                    <p className="text-xs text-surface-border mt-1">
                                        {isStoryboardingRunActive
                                            ? `${readyStoryboardClipCount} of ${totalStoryboardClipCount} frames are ready. Stay here while new shots appear automatically as they finish generating.`
                                            : "Review the exact starting frames for Veo 3.1. You can still add or delete shots, change durations, and rewrite descriptions here before rerunning storyboards or moving forward."}
                                    </p>
                                </div>
                                {isStagePlaybackPlaying && (
                                    <GlassCard className="!p-4 overflow-hidden">
                                        <div className="flex items-start justify-between gap-4 mb-4">
                                            <div>
                                                <div className="text-[11px] uppercase tracking-[0.18em] text-cyan-300/80">
                                                    Storyboard Slideshow
                                                </div>
                                                <div className="mt-1 text-sm font-semibold text-white/90">
                                                    {storyboardPlaybackClip
                                                        ? `Shot ${(project.timeline.findIndex((clip) => clip.id === storyboardPlaybackClip.id) + 1)}`
                                                        : "Preparing storyboard playback"}
                                                </div>
                                                <p className="mt-1 text-xs text-surface-border">
                                                    Playback follows the footer timeline. The slideshow hides as soon as playback stops.
                                                </p>
                                            </div>
                                            <div className="text-xs font-mono text-surface-border">
                                                {formatTransportTime(stagePlaybackSeconds)} / {formatTransportTime(stageTimelineDuration)}
                                            </div>
                                        </div>
                                        <div className="flex flex-col gap-4 lg:flex-row">
                                            <div className="flex-1 min-h-[20rem] rounded-2xl border border-surface-border bg-black/40 overflow-hidden flex items-center justify-center">
                                                {storyboardPlaybackClip?.image_url ? (
                                                    <img
                                                        src={toBackendAssetUrl(storyboardPlaybackClip.image_url)}
                                                        alt={`Storyboard playback shot ${project.timeline.findIndex((clip) => clip.id === storyboardPlaybackClip.id) + 1}`}
                                                        className="w-full h-full object-contain"
                                                    />
                                                ) : (
                                                    <div className="px-6 text-center text-surface-border">
                                                        <ImageIcon className="w-10 h-10 mx-auto mb-3 opacity-60" />
                                                        <p className="text-sm">No storyboard frame is available for this beat yet.</p>
                                                    </div>
                                                )}
                                            </div>
                                            <div className="w-full lg:w-[20rem] rounded-2xl border border-surface-border bg-background/35 p-4">
                                                <div className="text-[11px] uppercase tracking-[0.18em] text-white/55">Current Prompt</div>
                                                <p className="mt-3 text-sm leading-6 text-white/85 whitespace-pre-wrap">
                                                    {storyboardPlaybackClip?.storyboard_text?.trim() || "No storyboard prompt is attached to this shot."}
                                                </p>
                                            </div>
                                        </div>
                                    </GlassCard>
                                )}
                                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5 gap-6 pb-12">
                                    {project.timeline.map((clip, index) => (
                                        <div
                                            key={clip.id}
                                            className={`relative p-4 bg-surface/40 backdrop-blur border rounded-xl cursor-grab active:cursor-grabbing transition-shadow ${selectedPlanningShotId === clip.id
                                                ? 'border-primary ring-2 ring-primary/40 bg-primary/10 md:col-span-2 xl:col-span-2'
                                                : 'border-surface-border hover:ring-1 ring-primary/30'
                                                }`}
                                            draggable
                                            onClick={() => setSelectedPlanningShotId(clip.id)}
                                            onDragStart={(e) => {
                                                dragSrc.current = index;
                                                e.dataTransfer.effectAllowed = 'move';
                                                e.currentTarget.style.opacity = '0.5';
                                            }}
                                            onDragEnter={(e) => {
                                                e.preventDefault();
                                                dragOver.current = index;
                                            }}
                                            onDragOver={(e) => e.preventDefault()}
                                            onDragEnd={(e) => {
                                                e.currentTarget.style.opacity = '1';
                                                if (dragSrc.current !== null && dragOver.current !== null && dragSrc.current !== dragOver.current) {
                                                    const newClips = [...project.timeline];
                                                    const dragged = newClips.splice(dragSrc.current, 1)[0];
                                                    newClips.splice(dragOver.current, 0, dragged);
                                                    handleStoryboardTimelineChange(newClips);
                                                }
                                                dragSrc.current = null;
                                                dragOver.current = null;
                                            }}
                                        >
                                            <div className="flex items-center gap-2 mb-3">
                                                <span className="flex items-center text-xs font-semibold text-primary uppercase tracking-wider">
                                                    <GripVertical className="w-3.5 h-3.5 mr-1 opacity-50" />
                                                    Shot {index + 1}
                                                </span>
                                                <div className="flex items-center gap-1 rounded-md border border-surface-border bg-background/40 px-2 py-1">
                                                    <label className="sr-only" htmlFor={`storyboard-duration-${clip.id}`}>Shot duration</label>
                                                    <select
                                                        id={`storyboard-duration-${clip.id}`}
                                                        value={quantizeStoryboardDuration(clip.duration)}
                                                        onChange={(e) => handleStoryboardDurationChange(clip.id, Number(e.target.value))}
                                                        onClick={(e) => e.stopPropagation()}
                                                        className="bg-transparent text-xs font-mono text-white/80 outline-none"
                                                        title="Shot duration"
                                                    >
                                                        {STORYBOARD_VALID_DURATIONS.map((duration) => (
                                                            <option key={duration} value={duration} className="text-black">
                                                                {duration}s
                                                            </option>
                                                        ))}
                                                    </select>
                                                </div>
                                                <div className="ml-auto flex items-center gap-1.5">
                                                    <button
                                                        type="button"
                                                        onClick={(e) => {
                                                            e.stopPropagation();
                                                            void handleStoryboardRegenerate(clip.id);
                                                        }}
                                                        disabled={!isCurrentDisplayedStage || isBusy || storyboardRegeneratingClipId === clip.id}
                                                        className={`rounded-md border p-1.5 transition-colors ${!isCurrentDisplayedStage || isBusy
                                                            ? 'border-surface-border bg-background/40 text-white/30 cursor-not-allowed'
                                                            : 'border-amber-500/25 bg-amber-500/10 text-amber-200 hover:bg-amber-500/20 hover:text-amber-100'
                                                            }`}
                                                        title={isCurrentDisplayedStage ? "Regenerate this storyboard frame now" : "Rewind the pipeline here to regenerate this shot"}
                                                    >
                                                        <RefreshCw className={`w-3.5 h-3.5 ${storyboardRegeneratingClipId === clip.id ? 'animate-spin' : ''}`} />
                                                    </button>
                                                    <button
                                                        type="button"
                                                        onClick={(e) => {
                                                            e.stopPropagation();
                                                            void handleStoryboardAiFill(clip, index);
                                                        }}
                                                        disabled={storyboardAiFillClipId === clip.id}
                                                        className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] font-semibold transition-colors ${storyboardAiFillClipId === clip.id
                                                            ? 'border-primary/30 bg-primary/10 text-primary/70 cursor-wait'
                                                            : 'border-primary/30 bg-primary/10 text-primary hover:bg-primary/20'
                                                            }`}
                                                        title="Ask Gemini to expand or write this shot"
                                                    >
                                                        {storyboardAiFillClipId === clip.id ? (
                                                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                                        ) : (
                                                            <Wand2 className="w-3.5 h-3.5" />
                                                        )}
                                                        {storyboardAiFillClipId === clip.id ? "Generating..." : "AI Fill"}
                                                    </button>
                                                    <button
                                                        type="button"
                                                        onClick={(e) => {
                                                            e.stopPropagation();
                                                            setSelectedPlanningShotId((prev) => prev === clip.id ? null : clip.id);
                                                        }}
                                                        className={`rounded-md border p-1.5 transition-colors ${selectedPlanningShotId === clip.id
                                                            ? 'border-primary/40 bg-primary/15 text-primary'
                                                            : 'border-surface-border bg-background/40 text-white/60 hover:text-white hover:bg-surface-hover'
                                                            }`}
                                                        title={selectedPlanningShotId === clip.id ? "Collapse shot" : "Expand shot"}
                                                    >
                                                        <Maximize2 className="w-3.5 h-3.5" />
                                                    </button>
                                                    <button
                                                        type="button"
                                                        onClick={(e) => {
                                                            e.stopPropagation();
                                                            handleStoryboardDeleteClip(clip.id);
                                                        }}
                                                        disabled={project.timeline.length <= 1}
                                                        className={`rounded-md border p-1.5 transition-colors ${project.timeline.length <= 1
                                                            ? 'border-rose-500/15 text-rose-400/30 cursor-not-allowed'
                                                            : 'border-rose-500/25 text-rose-300 hover:bg-rose-500/10 hover:text-rose-200'
                                                            }`}
                                                        title="Delete this shot"
                                                    >
                                                        <Trash2 className="w-3.5 h-3.5" />
                                                    </button>
                                                </div>
                                            </div>
                                            {/* Image Display */}
                                            <div className="mb-4 rounded overflow-hidden relative bg-black/50 aspect-video flex items-center justify-center border border-surface-border group">
                                                {clip.image_url ? (
                                                    <>
                                                        <button
                                                            type="button"
                                                            onClick={(e) => {
                                                                e.stopPropagation();
                                                                setZoomedStoryboardImage({
                                                                    url: toBackendAssetUrl(clip.image_url),
                                                                    label: `Shot ${index + 1}`,
                                                                });
                                                            }}
                                                            className="absolute inset-0 z-0 cursor-zoom-in"
                                                            title={`Open Shot ${index + 1} larger`}
                                                        >
                                                            <img
                                                                src={toBackendAssetUrl(clip.image_url)}
                                                                alt={`Clip ${index + 1}`}
                                                                className="w-full h-full object-cover"
                                                                draggable={false}
                                                            />
                                                        </button>
                                                        <div className="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/65 via-black/10 to-transparent px-3 py-2 text-[11px] text-white/75 opacity-0 transition-opacity group-hover:opacity-100">
                                                            Click to enlarge
                                                        </div>
                                                        <a
                                                            href={toBackendAssetUrl(clip.image_url)}
                                                            download={`shot_${index + 1}.png`}
                                                            target="_blank"
                                                            rel="noreferrer"
                                                            className="absolute top-2 right-2 z-10 p-1.5 bg-black/60 hover:bg-black/80 rounded backdrop-blur-sm text-white/70 hover:text-white transition-all opacity-0 group-hover:opacity-100"
                                                            title="Save Image"
                                                            onClick={(e) => e.stopPropagation()}
                                                        >
                                                            <Save className="w-4 h-4" />
                                                        </a>
                                                        {storyboardRegeneratingClipId === clip.id && (
                                                            <div className="absolute inset-0 z-20 flex flex-col items-center justify-center bg-black/65 text-white/80 text-xs">
                                                                <Loader2 className="w-6 h-6 animate-spin mb-2" />
                                                                Regenerating frame...
                                                            </div>
                                                        )}
                                                    </>
                                                ) : isStoryboardingRunActive ? (
                                                    <div className="text-white/50 text-xs flex flex-col items-center p-4 text-center">
                                                        <Loader2 className="w-6 h-6 animate-spin mb-2" />
                                                        Generating with NanoBanana...
                                                    </div>
                                                ) : storyboardRegeneratingClipId === clip.id ? (
                                                    <div className="text-white/50 text-xs flex flex-col items-center p-4 text-center">
                                                        <Loader2 className="w-6 h-6 animate-spin mb-2" />
                                                        Regenerating frame...
                                                    </div>
                                                ) : isBusy ? (
                                                    <div className="text-white/50 text-xs flex flex-col items-center p-4 text-center">
                                                        <Loader2 className="w-6 h-6 animate-spin mb-2" />
                                                        Waiting...
                                                    </div>
                                                ) : (
                                                    <div className="text-rose-400/80 text-xs flex flex-col items-center text-center p-4">
                                                        <div className="w-8 h-8 mb-2 rounded-full bg-rose-500/20 flex items-center justify-center">!</div>
                                                        Generation Failed.
                                                    </div>
                                                )}
                                            </div>
                                            <div className="flex flex-col space-y-3 mb-3">
                                                <textarea
                                                    value={clip.storyboard_text}
                                                    onChange={(e) => handleClipEdit(clip.id, e.target.value)}
                                                    className="w-full min-h-[120px] bg-background/50 border border-surface-border rounded p-3 text-sm text-white/90 focus:outline-none focus:border-primary/50 resize-y leading-relaxed"
                                                    placeholder="Storyboard description..."
                                                    title={`Storyboard description for Clip ${index + 1}`}
                                                    onClick={(e) => e.stopPropagation()}
                                                />
                                                <input
                                                    type="file"
                                                    id={`storyboard-frame-upload-${clip.id}`}
                                                    className="hidden"
                                                    accept="image/*"
                                                    onChange={(e) => handleStoryboardFrameSwap(clip.id, e)}
                                                />
                                                <button
                                                    onClick={(e) => {
                                                        e.stopPropagation();
                                                        document.getElementById(`storyboard-frame-upload-${clip.id}`)?.click();
                                                    }}
                                                    disabled={!isCurrentDisplayedStage || isBusy || activeMediaSwapKey === `image:${clip.id}`}
                                                    title={isCurrentDisplayedStage ? "Upload a custom storyboard frame for this shot" : "Rewind the pipeline here to replace this shot"}
                                                    className={`w-full py-2 rounded text-xs font-medium transition-colors border ${!isCurrentDisplayedStage || isBusy ? 'border-surface-border text-white/30 cursor-not-allowed' : 'border-primary/30 text-primary hover:bg-primary/10'} ${activeMediaSwapKey === `image:${clip.id}` ? 'opacity-70 cursor-wait' : ''}`}
                                                >
                                                    {activeMediaSwapKey === `image:${clip.id}` ? (
                                                        <span className="flex items-center justify-center">
                                                            <Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" />
                                                            Uploading Frame...
                                                        </span>
                                                    ) : clip.image_url ? "Swap With Uploaded Frame" : "Upload Frame Instead"}
                                                </button>
                                                <div className="flex space-x-2">
                                                    <button
                                                        onClick={(e) => {
                                                            e.stopPropagation();
                                                            void handleImageApproval(clip.id, true);
                                                        }}
                                                        className={`flex-1 py-1.5 rounded text-xs font-medium transition-colors ${clip.image_approved === true ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30' : 'bg-surface-border hover:bg-surface-hover text-white/70'}`}
                                                    >
                                                        {clip.image_approved === true ? "Approved" : "Approve"}
                                                    </button>
                                                    <button
                                                        onClick={(e) => {
                                                            e.stopPropagation();
                                                            void handleImageApproval(clip.id, false);
                                                        }}
                                                        className={`flex-1 py-1.5 rounded text-xs font-medium transition-colors ${clip.image_approved === false ? 'bg-rose-500/20 text-rose-400 border border-rose-500/30' : 'bg-surface-border hover:bg-surface-hover text-white/70'}`}
                                                    >
                                                        {clip.image_approved === false ? "Rejected" : "Reject"}
                                                    </button>
                                                </div>
                                                {clip.image_approved !== true && clip.image_critiques.length > 0 && (
                                                    <div className="rounded border border-rose-500/20 bg-rose-500/10 p-2.5 text-[11px] text-rose-100/85">
                                                        <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-rose-300 mb-1">
                                                            Critic Notes
                                                        </div>
                                                        <p className="leading-relaxed break-words">
                                                            {clip.image_critiques[clip.image_critiques.length - 1]}
                                                        </p>
                                                    </div>
                                                )}
                                            </div>
                                        </div>
                                    ))}
                                    <button
                                        type="button"
                                        onClick={handleStoryboardAddClip}
                                        className="min-h-[24rem] rounded-xl border border-dashed border-surface-border bg-background/30 p-4 text-left transition-colors hover:border-primary/40 hover:bg-primary/5"
                                    >
                                        <div className="flex h-full flex-col items-center justify-center text-center">
                                            <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-full border border-primary/25 bg-primary/10 text-primary">
                                                <Plus className="w-5 h-5" />
                                            </div>
                                            <div className="text-sm font-semibold text-white/90">Add Shot</div>
                                            <p className="mt-2 max-w-[16rem] text-xs leading-relaxed text-surface-border">
                                                Insert a new storyboard card directly here instead of jumping back to the Planning editor.
                                            </p>
                                        </div>
                                    </button>
                                </div>
                            </div>
                        )}

                        {displayStage === 'filming' && (
                            <div className="space-y-4 mb-6">
                                <div className="p-3 bg-surface-hover/30 border border-primary/20 rounded-xl mb-4">
                                    <p className="text-sm text-white/80 font-medium">{isFilmingRunActive ? "Veo 3.1 is generating clips." : "Veo 3.1 Video Renders Ready."}</p>
                                    <p className="text-xs text-surface-border mt-1">
                                        {isFilmingRunActive
                                            ? `${readyFilmingClipCount} of ${totalFilmingClipCount} clips are ready. Stay here while new shots appear automatically as they finish rendering.`
                                            : "Review the final clips. Approve them to move into Production, reject individual clips to rerender them, or upload your own replacement clip for any shot."}
                                    </p>
                                </div>
                                <div className="grid grid-cols-1 gap-4">
                                    {project.timeline.map((clip, index) => {
                                        const evaluatorSummary = getFilmingEvaluatorSummary(clip);
                                        return (
                                            <GlassCard
                                                key={clip.id}
                                                className={`relative !p-3 cursor-pointer transition-colors ${selectedFilmingClipId === clip.id ? 'ring-2 ring-primary bg-primary/10' : 'hover:bg-surface-hover/50'}`}
                                                onClick={() => setSelectedFilmingClipId((current) => current === clip.id ? null : clip.id)}
                                            >
                                                <div className="flex justify-between items-center mb-2">
                                                    <span className="text-xs font-semibold text-primary uppercase tracking-wider">Clip {index + 1}</span>
                                                    <span className="text-xs text-surface-border font-mono">{clip.duration.toFixed(1)}s</span>
                                                </div>
                                                <div className="mb-3 rounded overflow-hidden relative bg-black/50 aspect-video flex items-center justify-center border border-surface-border group pointer-events-none">
                                                    {clip.video_url ? (
                                                        <>
                                                            <video
                                                                src={toBackendAssetUrl(clip.video_url)}
                                                                className="w-full h-full object-cover opacity-70 group-hover:opacity-100 transition-opacity"
                                                                autoPlay
                                                                loop
                                                                muted
                                                            />
                                                            <div className="absolute inset-0 flex items-center justify-center">
                                                                <div className="w-10 h-10 rounded-full bg-black/50 backdrop-blur flex items-center justify-center group-hover:scale-110 transition-transform">
                                                                    <Play className="w-4 h-4 text-white ml-1" fill="currentColor" />
                                                                </div>
                                                            </div>
                                                            <a
                                                                href={toBackendAssetUrl(clip.video_url)}
                                                                download={`clip_${index + 1}.mp4`}
                                                                target="_blank"
                                                                rel="noreferrer"
                                                                className="absolute top-2 right-2 p-1.5 bg-black/60 hover:bg-black/80 rounded backdrop-blur-sm text-white/70 hover:text-white transition-all opacity-0 group-hover:opacity-100 z-10 pointer-events-auto"
                                                                title="Save Video Clip"
                                                                onClick={(e) => e.stopPropagation()}
                                                            >
                                                                <Save className="w-4 h-4" />
                                                            </a>
                                                        </>
                                                    ) : isFilmingRunActive ? (
                                                        <div className="text-white/50 text-xs flex flex-col items-center">
                                                            <Loader2 className="w-6 h-6 animate-spin mb-2" />
                                                            Rendering with Veo 3.1...
                                                        </div>
                                                    ) : (
                                                        <div className="text-rose-400/80 text-xs flex flex-col items-center text-center p-4">
                                                            <div className="w-8 h-8 mb-2 rounded-full bg-rose-500/20 flex items-center justify-center">!</div>
                                                            Generation Failed.<br />Check terminal logs or retry.
                                                        </div>
                                                    )}
                                                </div>
                                                <div className="flex flex-col space-y-3 mb-3">
                                                    <textarea
                                                        value={clip.video_prompt ?? ''}
                                                        onChange={(e) => {
                                                            if (!project) return;
                                                            const newTimeline = project.timeline.map(c => c.id === clip.id ? { ...c, video_prompt: e.target.value } : c);
                                                            setProject({ ...project, timeline: newTimeline });
                                                        }}
                                                        disabled={isBusy}
                                                        className="w-full min-h-[60px] bg-background/50 border border-surface-border rounded p-2 text-xs text-white/90 focus:outline-none focus:border-primary/50 resize-y pointer-events-auto"
                                                        placeholder="Veo specific instructions..."
                                                        title={`Veo directions for Clip ${index + 1}`}
                                                        onClick={(e) => e.stopPropagation()}
                                                    />
                                                    <input
                                                        type="file"
                                                        id={`filming-clip-upload-${clip.id}`}
                                                        className="hidden"
                                                        accept="video/*"
                                                        onChange={(e) => handleFilmingClipSwap(clip.id, e)}
                                                    />
                                                    <button
                                                        onClick={(e) => {
                                                            e.stopPropagation();
                                                            document.getElementById(`filming-clip-upload-${clip.id}`)?.click();
                                                        }}
                                                        disabled={!isCurrentDisplayedStage || isBusy || activeMediaSwapKey === `video:${clip.id}`}
                                                        title={isCurrentDisplayedStage ? "Upload a custom video clip for this shot" : "Rewind the pipeline here to replace this clip"}
                                                        className={`w-full py-2 rounded text-xs font-medium transition-colors border pointer-events-auto ${!isCurrentDisplayedStage || isBusy ? 'border-surface-border text-white/30 cursor-not-allowed' : 'border-primary/30 text-primary hover:bg-primary/10'} ${activeMediaSwapKey === `video:${clip.id}` ? 'opacity-70 cursor-wait' : ''}`}
                                                    >
                                                        {activeMediaSwapKey === `video:${clip.id}` ? (
                                                            <span className="flex items-center justify-center">
                                                                <Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" />
                                                                Uploading Clip...
                                                            </span>
                                                        ) : clip.video_url ? "Swap With Uploaded Clip" : "Upload Clip Instead"}
                                                    </button>
                                                    <div className="flex space-x-2 pointer-events-auto">
                                                        <button
                                                            onClick={(e) => { e.stopPropagation(); handleVideoApproval(clip.id, true); }}
                                                            disabled={!clip.video_url}
                                                            className={`flex-1 py-1.5 rounded text-xs font-medium transition-colors ${clip.video_approved === true ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30' : 'bg-surface-border hover:bg-surface-hover text-white/70'} ${!clip.video_url ? 'opacity-40 cursor-not-allowed' : ''}`}
                                                        >
                                                            {clip.video_approved === true ? "Approved" : "Approve"}
                                                        </button>
                                                        <button
                                                            onClick={(e) => { e.stopPropagation(); handleVideoApproval(clip.id, false); }}
                                                            className={`flex-1 py-1.5 rounded text-xs font-medium transition-colors ${clip.video_approved === false ? 'bg-rose-500/20 text-rose-400 border border-rose-500/30' : 'bg-surface-border hover:bg-surface-hover text-white/70'}`}
                                                        >
                                                            {clip.video_approved === false ? "Rejected" : "Reject / Regenerate"}
                                                        </button>
                                                    </div>
                                                    {evaluatorSummary && (
                                                        <div
                                                            className={`rounded border p-2 text-[11px] ${
                                                                evaluatorSummary.tone === "success"
                                                                    ? "border-emerald-500/20 bg-emerald-500/10 text-emerald-100/85"
                                                                    : evaluatorSummary.tone === "error"
                                                                        ? "border-rose-500/20 bg-rose-500/10 text-rose-100/85"
                                                                        : "border-amber-500/20 bg-amber-500/10 text-amber-100/85"
                                                            }`}
                                                        >
                                                            <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.18em]">
                                                                Evaluator Summary
                                                            </div>
                                                            {evaluatorSummary.text}
                                                        </div>
                                                    )}
                                                </div>
                                            </GlassCard>
                                        );
                                    })}
                                </div>
                            </div>
                        )}

                        {displayStage === 'production' && (
                            <div className="space-y-4 mb-6">
                                <div className="p-3 bg-surface-hover/30 border border-primary/20 rounded-xl">
                                    <p className="text-sm text-white/80 font-medium">Production Timeline Ready.</p>
                                    <p className="text-xs text-surface-border mt-1">Split clips or music at the playhead, drag `V1` to reorder picture, drag `M1` to position music, then click `A1` to delete or restore source audio on any fragment before rendering.</p>
                                </div>
                                {project.last_error && (
                                    <div className="p-3 bg-rose-500/10 border border-rose-500/30 rounded-xl">
                                        <p className="text-sm text-rose-300 font-medium">Final render failed.</p>
                                        <p className="text-xs text-rose-200/80 mt-1 font-mono break-words">{project.last_error}</p>
                                    </div>
                                )}
                                <GlassCard className="!p-4 space-y-4">
                                    <div className="flex items-center justify-between gap-4">
                                        <div>
                                            <h3 className="text-sm font-semibold text-white/90">Selected Edit</h3>
                                            <p className="text-xs text-surface-border mt-1">`V1` and `A1` stay time-linked, while `M1` is independently draggable and splittable.</p>
                                        </div>
                                        <div className="text-xs text-surface-border font-mono">
                                            Program Length: {productionDuration.toFixed(1)}s
                                        </div>
                                    </div>
                                    {!project.final_video_url && (
                                        <div className="text-xs text-surface-border">
                                            Render the timeline to export a final master from Production.
                                        </div>
                                    )}
                                    {selectedProductionFragment ? (
                                        <React.Fragment>
                                            <div className="grid grid-cols-2 gap-3 text-xs">
                                                <div className="rounded-lg border border-surface-border bg-background/50 p-3">
                                                    <div className="text-surface-border uppercase tracking-[0.18em] mb-1">Source Shot</div>
                                                    <div className="text-white/90 font-medium">
                                                        {(selectedProductionFragment.track_type ?? "video") === "music"
                                                            ? "Music Track"
                                                            : selectedProductionFragment.source_clip_id}
                                                    </div>
                                                </div>
                                                <div className="rounded-lg border border-surface-border bg-background/50 p-3">
                                                    <div className="text-surface-border uppercase tracking-[0.18em] mb-1">Timeline In</div>
                                                    <div className="text-white/90 font-medium">{selectedProductionFragment.timeline_start.toFixed(1)}s</div>
                                                </div>
                                                <div className="rounded-lg border border-surface-border bg-background/50 p-3">
                                                    <div className="text-surface-border uppercase tracking-[0.18em] mb-1">Source In</div>
                                                    <div className="text-white/90 font-medium">{selectedProductionFragment.source_start.toFixed(1)}s</div>
                                                </div>
                                                <div className="rounded-lg border border-surface-border bg-background/50 p-3">
                                                    <div className="text-surface-border uppercase tracking-[0.18em] mb-1">Fragment Duration</div>
                                                    <div className="text-white/90 font-medium">{selectedProductionFragment.duration.toFixed(1)}s</div>
                                                </div>
                                                <div className="rounded-lg border border-surface-border bg-background/50 p-3">
                                                    <div className="text-surface-border uppercase tracking-[0.18em] mb-1">Selected Track</div>
                                                    <div className="text-white/90 font-medium">
                                                        {selectedProductionTrack === "audio"
                                                            ? "A1 Source Audio"
                                                            : selectedProductionTrack === "music"
                                                                ? "M1 Music"
                                                                : "V1 Picture"}
                                                    </div>
                                                </div>
                                                <div className="rounded-lg border border-surface-border bg-background/50 p-3">
                                                    <div className="text-surface-border uppercase tracking-[0.18em] mb-1">
                                                        {selectedProductionTrack === "music" ? "Music Status" : "A1 Status"}
                                                    </div>
                                                    <div className={`font-medium ${(selectedProductionFragment.audio_enabled ?? true) ? "text-emerald-300" : "text-rose-300"}`}>
                                                        {selectedProductionTrack === "music"
                                                            ? "Music segment active"
                                                            : (selectedProductionFragment.audio_enabled ?? true)
                                                                ? "Source audio enabled"
                                                                : "Source audio removed"}
                                                    </div>
                                                </div>
                                            </div>
                                            <div className="flex flex-wrap gap-2">
                                                <button
                                            onClick={handleToggleSelectedProductionAudio}
                                            disabled={!canToggleSelectedAudio || isBusy}
                                            className={`inline-flex items-center justify-center rounded-lg border px-4 py-2 text-sm font-medium transition-colors ${canToggleSelectedAudio && !isBusy
                                                ? (selectedProductionFragment.audio_enabled ?? true)
                                                    ? "border-rose-500/30 bg-rose-500/10 text-rose-300 hover:bg-rose-500/20"
                                                    : "border-emerald-500/30 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20"
                                                        : "border-surface-border bg-background/40 text-white/30 cursor-not-allowed"
                                                        }`}
                                                >
                                                    {(selectedProductionFragment.audio_enabled ?? true) ? "Delete Audio From Selection" : "Restore Audio To Selection"}
                                                </button>
                                                <div className="text-xs text-surface-border self-center">
                                                    {selectedProductionTrack === "music"
                                                        ? "Split or drag the `M1` block to reposition the music bed against your picture edit."
                                                        : selectedProductionTrack === "audio"
                                                        ? "Editing A1: this only affects the selected fragment's source audio."
                                                        : "Select the `A1` fragment below to remove or restore audio independently of picture."}
                                                </div>
                                            </div>
                                        </React.Fragment>
                                    ) : (
                                        <div className="rounded-lg border border-dashed border-surface-border bg-background/30 p-4 text-xs text-surface-border text-center">
                                            Select a fragment on the production timeline to inspect it here.
                                        </div>
                                    )}
                                </GlassCard>
                            </div>
                        )}

                        {displayStage === 'completed' && (
                            <div className="space-y-4 mb-6">
                                <div className="p-3 bg-emerald-500/20 border border-emerald-500/30 rounded-xl">
                                    <p className="text-sm text-emerald-400 font-medium">Production Complete!</p>
                                    <p className="text-xs text-emerald-500/70 mt-1">Your AI-generated music video has been rendered from the production edit timeline.</p>
                                </div>
                                <GlassCard className="!p-6 text-center">
                                    <Video className="w-12 h-12 text-primary mx-auto mb-3" />
                                    <h3 className="font-medium text-white/90 mb-1">Final Render Ready</h3>
                                    <p className="text-xs text-surface-border mb-6">Watch it in the preview pane, download it, or click the Production step above to reopen the timeline.</p>
                                    {project.final_video_url ? (
                                        <button
                                            onClick={() => void handleExportFinalVideo()}
                                            disabled={isExportingFinalVideo}
                                            className={`inline-flex items-center justify-center w-full px-4 py-3 text-white text-sm font-medium rounded-lg transition-colors shadow-lg shadow-primary/20 ${isExportingFinalVideo
                                                ? "bg-surface-border cursor-not-allowed opacity-60"
                                                : "bg-primary hover:bg-primary-hover"
                                                }`}
                                        >
                                            {isExportingFinalVideo ? "Preparing Download..." : "Download Master Video"}
                                        </button>
                                    ) : (
                                        <div className="text-xs text-surface-border">No final render is currently attached to this project.</div>
                                    )}
                                </GlassCard>
                            </div>
                        )}

                        {/* Navigation: Back / main action / Redo */}
                        <div className="flex gap-2 items-stretch mt-4">
                            {/* ← Back: go to previous stage */}
                            {displayStage !== 'input' && displayStage === project.current_stage && (
                                <button
                                    onClick={() => handleRevert(getPreviousReviewStage(displayStage))}
                                    disabled={false}
                                    title="Hard stop everything and go back"
                                    className="flex-none px-4 py-3 rounded-lg font-medium transition-colors border border-rose-500/30 hover:bg-rose-500/20 text-rose-400 hover:text-white shadow-[0_0_15px_rgba(244,63,94,0.1)] text-sm"
                                >
                                    ← Stop & Back
                                </button>
                            )}

                            {/* Main action button */}
                            {displayStage !== project.current_stage ? (
                                <div className="flex flex-1 gap-2">
                                    <button
                                        onClick={() => handleRevert(displayStage)}
                                        disabled={isBusy}
                                        className="flex-none px-4 py-3 rounded-lg font-medium transition-colors border border-amber-500/30 hover:bg-amber-500/20 text-amber-500 hover:text-white"
                                        title="Rewind the ADK pipeline to this step. Destructive to future steps."
                                    >
                                        ⏪ Rewind Pipeline Here
                                    </button>
                                    <button
                                        onClick={handleSaveChanges}
                                        disabled={isBusy}
                                        className="flex-1 text-white py-3 rounded-lg font-medium transition-colors shadow-lg bg-emerald-600 hover:bg-emerald-500 shadow-emerald-500/20 text-sm"
                                        title="Save the current project state"
                                    >
                                        💾 Save Project
                                    </button>
                                    <button
                                        onClick={() => setViewedStage(null)}
                                        className="flex-1 text-white py-3 rounded-lg font-medium transition-colors shadow-lg bg-surface-border hover:bg-surface-hover text-sm"
                                    >
                                        Return to Active Stage
                                    </button>
                                </div>
                            ) : (
                                project.current_stage === 'lyria_prompting' ? (
                                    <div className="flex flex-1 gap-2">
                                        <button
                                            onClick={handleRunPipeline}
                                            disabled={isBusy || !canContinueFromMusicStage}
                                            className={`flex-1 text-white py-3 rounded-lg font-medium transition-colors shadow-lg ${(isBusy || !canContinueFromMusicStage) ? "bg-surface-border opacity-50 cursor-not-allowed" : "bg-primary hover:bg-primary-hover shadow-primary/20"
                                                }`}
                                        >
                                            {isBusy ? (
                                                <span className="flex items-center justify-center">
                                                    <Loader2 className="w-5 h-5 mr-2 animate-spin" />
                                                    Processing...
                                                </span>
                                            ) : usesManualMusicImport
                                                ? (project.music_url ? "Use Imported Song & Build Timeline" : "Import Song To Continue")
                                                : hasCurrentAutomaticMusicTrack
                                                    ? "Use Current Song & Build Timeline"
                                                    : project.music_url
                                                        ? "Regenerate Song To Continue"
                                                        : "Generate Song To Continue"}
                                        </button>
                                        {usesManualMusicImport ? (
                                            <React.Fragment>
                                                <button
                                                    onClick={handleImportSong}
                                                    disabled={isBusy}
                                                    className={`flex-none px-4 py-3 rounded-lg font-medium transition-colors border text-sm ${isBusy
                                                        ? "border-surface-border text-white/30 cursor-not-allowed"
                                                        : "border-cyan-500/30 bg-cyan-500/10 text-cyan-200 hover:bg-cyan-500/20"
                                                        }`}
                                                >
                                                    {project.music_url ? "Replace Imported Song" : "Import Song"}
                                                </button>
                                                <button
                                                    onClick={() => void handleCopyMusicPromptPackage()}
                                                    disabled={isBusy}
                                                    className={`flex-none px-4 py-3 rounded-lg font-medium transition-colors border text-sm ${isBusy
                                                        ? "border-surface-border text-white/30 cursor-not-allowed"
                                                        : "border-amber-500/30 bg-amber-500/10 text-amber-200 hover:bg-amber-500/20"
                                                        }`}
                                                >
                                                    Copy Prompt Package
                                                </button>
                                            </React.Fragment>
                                        ) : (
                                            <button
                                                onClick={handleRegenerateMusic}
                                                disabled={isBusy}
                                                className={`flex-none px-4 py-3 rounded-lg font-medium transition-colors border text-sm ${isBusy
                                                    ? "border-surface-border text-white/30 cursor-not-allowed"
                                                    : "border-cyan-500/30 bg-cyan-500/10 text-cyan-200 hover:bg-cyan-500/20"
                                                    }`}
                                            >
                                                {isRegeneratingMusic
                                                    ? "Generating..."
                                                    : project.music_url
                                                        ? "Regenerate Song"
                                                        : "Generate Song"}
                                            </button>
                                        )}
                                    </div>
                                ) : (
                                    <button
                                        onClick={handleRunPipeline}
                                        disabled={isBusy}
                                        className={`flex-1 text-white py-3 rounded-lg font-medium transition-colors shadow-lg ${isBusy ? "bg-surface-border opacity-50 cursor-not-allowed" : "bg-primary hover:bg-primary-hover shadow-primary/20"
                                            }`}
                                    >
                                        {isBusy ? (
                                            <span className="flex items-center justify-center">
                                                <Loader2 className="w-5 h-5 mr-2 animate-spin" />
                                                {isStoryboardingRunActive
                                                    ? "Generating Frames..."
                                                    : isFilmingRunActive
                                                        ? "Generating Clips..."
                                                        : "Processing..."}
                                            </span>
                                        ) : project.current_stage === 'input' ? "Start ADK Pipeline"
                                            : project.current_stage === 'planning' ? "Generate Storyboards (NanoBanana)"
                                                : project.current_stage === 'storyboarding' && project.timeline.every(clip => clip.image_approved === true) ? "Generate Videos (Veo 3.1)"
                                                    : project.current_stage === 'storyboarding' && project.timeline.some(clip => clip.image_approved === false) ? "Regenerate Rejected Storyboards"
                                                        : project.current_stage === 'storyboarding' ? "Review Storyboards"
                                                            : project.current_stage === 'filming' && project.timeline.every(clip => clip.video_approved === true && !!clip.video_url) ? "Open Production Timeline"
                                                                : project.current_stage === 'filming' && project.timeline.some(clip => clip.video_approved === false) ? "Regenerate Rejected Clips"
                                                                    : project.current_stage === 'filming' ? "Review Video Renders"
                                                                        : project.current_stage === 'production' ? "Render Final Video"
                                                                            : "Return to Dashboard"}
                                    </button>
                                )
                            )}
                        </div>
                        {displayStage === 'production' && (project.final_video_url || hasExportableResources) && (
                            <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
                                {project.final_video_url && (
                                    <button
                                        onClick={() => void handleExportFinalVideo()}
                                        disabled={isExportingFinalVideo}
                                        className={`inline-flex w-full items-center justify-center rounded-xl border px-4 py-3.5 text-sm font-semibold transition-colors ${isExportingFinalVideo
                                            ? "border-surface-border bg-surface-border text-white/40 cursor-not-allowed"
                                            : "border-emerald-500/30 bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25"
                                            }`}
                                    >
                                        {isExportingFinalVideo ? "Preparing Final Clip..." : "Export Current Final Clip"}
                                    </button>
                                )}
                                {hasExportableResources && (
                                    <button
                                        onClick={handleExportResources}
                                        disabled={isExportingResources}
                                        className={`inline-flex w-full items-center justify-center rounded-xl border px-4 py-3.5 text-sm font-semibold transition-colors ${isExportingResources
                                            ? "border-surface-border bg-surface-border text-white/40 cursor-not-allowed"
                                            : "border-cyan-500/30 bg-cyan-500/15 text-cyan-200 hover:bg-cyan-500/25"
                                            }`}
                                    >
                                        {isExportingResources ? "Exporting Resources..." : "Export All Resources"}
                                    </button>
                                )}
                            </div>
                        )}
                    </div>
                </section>

                {/* Right pane: Preview (Hidden conditionally on full-width views) */}
                {
                    (displayStage !== 'input' && displayStage !== 'storyboarding') && (
                        <section className="flex-1 min-h-0 m-4 ml-0 glass rounded-xl flex items-center justify-center flex-col relative overflow-hidden z-10 border border-surface-border/50">
                            {displayStage === 'planning' && selectedPlanningShotId ? (
                                <div className="w-full h-full flex flex-col p-6 bg-surface/30">
                                    <div className="flex items-center gap-3 mb-4">
                                        <h3 className="text-xl font-semibold text-white/90">Shot Editor</h3>
                                        <span className="px-2 py-0.5 rounded text-xs font-mono bg-primary/20 text-primary uppercase">
                                            Shot {(project.timeline.findIndex(c => c.id === selectedPlanningShotId) + 1)}
                                        </span>
                                        <button
                                            onClick={() => setSelectedPlanningShotId(null)}
                                            className="ml-auto p-2 rounded-full hover:bg-surface-hover text-surface-border hover:text-white transition-colors"
                                        >
                                            <X className="w-4 h-4" />
                                        </button>
                                    </div>
                                    <textarea
                                        className="flex-1 w-full bg-background/50 border border-surface-border rounded-xl p-6 text-base text-white/90 focus:outline-none focus:border-primary/50 resize-none font-sans leading-relaxed shadow-inner"
                                        placeholder="Describe the shot in detail here..."
                                        value={project.timeline.find(c => c.id === selectedPlanningShotId)?.storyboard_text || ''}
                                        onChange={(e) => {
                                            const updatedTimeline = project.timeline.map(c =>
                                                c.id === selectedPlanningShotId ? { ...c, storyboard_text: e.target.value } : c
                                            );
                                            setProject({ ...project, timeline: updatedTimeline });
                                        }}
                                    />
                                </div>
                            ) : displayStage === 'filming' ? (
                                isStagePlaybackPlaying ? (
                                    <div className="w-full h-full flex flex-col p-6 bg-surface/30">
                                        <div className="flex items-center gap-3 mb-4">
                                            <h3 className="text-xl font-semibold text-white/90">Filming Playback</h3>
                                            <span className="px-2 py-0.5 rounded text-xs font-mono bg-primary/20 text-primary uppercase">
                                                {filmingPlaybackClip
                                                    ? `Clip ${project.timeline.findIndex(c => c.id === filmingPlaybackClip.id) + 1}`
                                                    : "Sequence"}
                                            </span>
                                            <div className="ml-auto text-xs font-mono text-surface-border">
                                                {formatTransportTime(stagePlaybackSeconds)} / {formatTransportTime(stageTimelineDuration)}
                                            </div>
                                        </div>
                                        <div className="flex-1 rounded-xl border border-surface-border bg-black/50 overflow-hidden flex items-center justify-center">
                                            {filmingPlaybackClip?.video_url ? (
                                                <video
                                                    ref={filmingPlaybackVideoRef}
                                                    key={filmingPlaybackClip.id}
                                                    src={toBackendAssetUrl(filmingPlaybackClip.video_url)}
                                                    className="w-full h-full object-contain"
                                                    playsInline
                                                    onLoadedMetadata={(event) => {
                                                        const video = event.currentTarget;
                                                        const clipOffsetSeconds = Math.max(0, stagePlaybackSeconds - filmingPlaybackClip.timeline_start);
                                                        try {
                                                            video.currentTime = clipOffsetSeconds;
                                                        } catch {
                                                            // Ignore seek timing errors until the browser is ready.
                                                        }
                                                    }}
                                                />
                                            ) : (
                                                <div className="px-6 text-center text-surface-border">
                                                    <Video className="w-10 h-10 mx-auto mb-3 opacity-60" />
                                                    <p className="text-sm">No filmed clip is available for this beat yet.</p>
                                                </div>
                                            )}
                                        </div>
                                        <div className="mt-4 rounded-xl border border-surface-border bg-background/35 p-4">
                                            <div className="text-[11px] uppercase tracking-[0.18em] text-white/55">Current Evaluator Summary</div>
                                            <p className="mt-3 text-sm leading-6 text-white/85 whitespace-pre-wrap">
                                                {filmingPlaybackClip
                                                    ? (getFilmingEvaluatorSummary(filmingPlaybackClip)?.text ?? "Evaluator summary is not available for this clip yet.")
                                                    : "Playback is waiting for the next available clip."}
                                            </p>
                                        </div>
                                    </div>
                                ) : selectedFilmingClipId && project.timeline.find(c => c.id === selectedFilmingClipId)?.video_url ? (
                                    <div className="w-full h-full flex flex-col p-6 bg-surface/30">
                                        <div className="flex items-center gap-3 mb-4">
                                            <h3 className="text-xl font-semibold text-white/90">Media Player</h3>
                                            <span className="px-2 py-0.5 rounded text-xs font-mono bg-primary/20 text-primary uppercase">
                                                Clip {(project.timeline.findIndex(c => c.id === selectedFilmingClipId) + 1)}
                                            </span>
                                            <button
                                                onClick={() => setSelectedFilmingClipId(null)}
                                                className="ml-auto p-2 rounded-full hover:bg-surface-hover text-surface-border hover:text-white transition-colors"
                                            >
                                                <X className="w-4 h-4" />
                                            </button>
                                        </div>
                                        <div className="flex-1 bg-black/50 rounded-xl overflow-hidden border border-surface-border">
                                            <video
                                                key={selectedFilmingClipId}
                                                src={toBackendAssetUrl(project.timeline.find(c => c.id === selectedFilmingClipId)?.video_url)}
                                                className="w-full h-full object-contain"
                                                controls
                                                autoPlay
                                            />
                                        </div>
                                    </div>
                                ) : (
                                    <div className="w-full h-full flex flex-col items-center justify-center p-8 bg-surface/30 text-center">
                                        <div className="w-16 h-16 rounded-full border border-primary/25 bg-primary/10 text-primary flex items-center justify-center mb-5">
                                            <Video className="w-7 h-7" />
                                        </div>
                                        <h3 className="text-xl font-semibold text-white/90">Select a clip to inspect</h3>
                                        <p className="mt-3 max-w-md text-sm leading-6 text-surface-border">
                                            Filming no longer selects a shot by default. Click any clip card to open it here, or use the footer playback controls to preview the full sequence against the music in this window.
                                        </p>
                                    </div>
                                )
                            ) : displayStage === 'lyria_prompting' ? (
                                <div className="w-full h-full flex flex-col p-6 bg-surface/30">
                                    <div className="flex items-center gap-3 mb-4">
                                        <h3 className="text-xl font-semibold text-white/90">Lyric Sheet</h3>
                                        <span className="px-2 py-0.5 rounded text-xs font-mono bg-primary/20 text-primary uppercase">
                                            {musicStageStatusLabel}
                                        </span>
                                        {project.style_prompt?.trim() && (
                                            <span className="text-xs text-surface-border truncate max-w-[45%]">
                                                {project.style_prompt}
                                            </span>
                                        )}
                                    </div>
                                    <div className="flex-1 rounded-xl border border-surface-border bg-black/30 overflow-hidden">
                                        <div className="h-full overflow-y-auto p-8">
                                            <div className="max-w-3xl h-full flex flex-col space-y-4">
                                                <div className="text-[11px] uppercase tracking-[0.22em] text-cyan-300/80">
                                                    Lyrics Editor
                                                </div>
                                                <textarea
                                                    value={project.lyrics_prompt ?? ''}
                                                    onChange={(e) => setProject({ ...project, lyrics_prompt: e.target.value })}
                                                    className="flex-1 min-h-[24rem] w-full bg-transparent border border-surface-border/60 rounded-xl p-5 text-lg leading-8 text-white/90 font-medium focus:outline-none focus:border-cyan-400/50 resize-none"
                                                    placeholder="Poetic lyrics for your song..."
                                                />
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            ) : displayStage === 'production' ? (
                                <div className="w-full h-full flex flex-col p-6 bg-surface/30">
                                    <div className="flex items-center gap-3 mb-4">
                                        <h3 className="text-xl font-semibold text-white/90">Program Monitor</h3>
                                        <span className="px-2 py-0.5 rounded text-xs font-mono bg-primary/20 text-primary uppercase">
                                            {activeProductionFragment ? activeProductionFragment.source_clip_id : "No Edit"}
                                        </span>
                                        {activeProductionFragment && (
                                            <span className="text-xs text-surface-border font-mono">
                                                In {activeProductionFragment.source_start.toFixed(1)}s • {activeProductionFragment.duration.toFixed(1)}s
                                            </span>
                                        )}
                                    </div>
                                    <div className="flex-1 bg-black/50 rounded-xl overflow-hidden border border-surface-border relative">
                                        {activeProductionFragment && activeProductionMonitor.src ? (
                                            <React.Fragment>
                                            <video
                                                ref={productionVideoARef}
                                                src={productionMonitorSlots.a.src ?? undefined}
                                                className={`absolute inset-0 w-full h-full object-contain transition-opacity duration-150 ${activeProductionMonitorSlot === "a" ? "opacity-100" : "opacity-0 pointer-events-none"}`}
                                                playsInline
                                                preload="auto"
                                                onLoadedMetadata={() => handleProductionMonitorSlotLoadedMetadata("a")}
                                                onCanPlay={() => handleProductionMonitorSlotCanPlay("a")}
                                            />
                                            <video
                                                ref={productionVideoBRef}
                                                src={productionMonitorSlots.b.src ?? undefined}
                                                className={`absolute inset-0 w-full h-full object-contain transition-opacity duration-150 ${activeProductionMonitorSlot === "b" ? "opacity-100" : "opacity-0 pointer-events-none"}`}
                                                playsInline
                                                preload="auto"
                                                onLoadedMetadata={() => handleProductionMonitorSlotLoadedMetadata("b")}
                                                onCanPlay={() => handleProductionMonitorSlotCanPlay("b")}
                                            />
                                            {isProductionBuffering && (
                                                <div className="absolute inset-0 flex items-center justify-center bg-black/45 backdrop-blur-sm">
                                                    <div className="rounded-xl border border-white/10 bg-black/60 px-4 py-2 text-sm text-white/80">
                                                        Buffering next clip...
                                                    </div>
                                                </div>
                                            )}
                                            </React.Fragment>
                                        ) : (
                                            <div className="absolute inset-0 flex flex-col items-center justify-center text-surface-border">
                                                <Video className="w-12 h-12 mb-3 opacity-50" />
                                                <p className="text-sm">No fragment under the playhead.</p>
                                                <p className="text-xs opacity-60 mt-1">Move the playhead onto a clip in the timeline to preview it.</p>
                                            </div>
                                        )}
                                        {project.music_url && (
                                            <audio
                                                ref={productionMusicRef}
                                                src={toBackendAssetUrl(project.music_url)}
                                                preload="auto"
                                                onLoadedMetadata={(event) => {
                                                    const duration = Number.isFinite(event.currentTarget.duration)
                                                        ? event.currentTarget.duration
                                                        : 0;
                                                    setProductionMusicDuration(duration);
                                                }}
                                                onDurationChange={(event) => {
                                                    const duration = Number.isFinite(event.currentTarget.duration)
                                                        ? event.currentTarget.duration
                                                        : 0;
                                                    setProductionMusicDuration(duration);
                                                }}
                                            />
                                        )}
                                    </div>
                                </div>
                            ) : project.final_video_url && displayStage === 'completed' ? (
                                isPreparingFinalVideo ? (
                                    <div className="absolute inset-0 flex flex-col items-center justify-center text-surface-border">
                                        <Loader2 className="w-12 h-12 mb-3 animate-spin" />
                                        <p className="text-sm">Preparing final render preview...</p>
                                        <p className="text-xs opacity-60 mt-1">Loading the master in browser-safe chunks.</p>
                                    </div>
                                ) : finalVideoBlobUrl ? (
                                    <video
                                        src={finalVideoBlobUrl}
                                        className="w-full h-full object-contain"
                                        controls
                                        autoPlay
                                    />
                                ) : (
                                    <div className="absolute inset-0 flex flex-col items-center justify-center text-surface-border">
                                        <AlertCircle className="w-12 h-12 mb-3 opacity-70" />
                                        <p className="text-sm">Final render preview unavailable.</p>
                                        <p className="text-xs opacity-60 mt-1">Use the download button to export the master clip.</p>
                                    </div>
                                )
                            ) : (
                                <div className="absolute inset-0 flex flex-col items-center justify-center text-surface-border">
                                    <ImageIcon className="w-16 h-16 mb-4 opacity-50" />
                                    <p>Preview Window</p>
                                    <p className="text-sm opacity-50">
                                        {displayStage === 'planning' ? "Expand a shot to edit it here" : "Visualizations and edits will appear here"}
                                    </p>
                                </div>
                            )}
                        </section>
                    )}
                    </React.Fragment>
                )}
            </main>

            {/* Bottom Timeline */}
            {shouldShowFooter && (
            <footer className="h-80 min-h-72 shrink-0 border-t border-surface-border glass z-20 flex min-h-0 flex-col">
                {isAssetViewerOpen ? (
                    <LiveDirectorPanel
                        mode="docked"
                        currentStage={displayStage}
                        focusLabel={liveDirectorFocusLabel}
                        turns={project.director_log ?? []}
                        isBusy={isBusy}
                        isProcessing={isDirectorProcessing}
                        emptyStateText="No asset direction yet. Try “rename this asset to Mira Solis” or “delete this document reference.”"
                        composerPlaceholder="Edit the asset library. Try “rename this asset to Neon Alley rooftop.”"
                        composerHintText="Enter to send. Use the mic to rename or prune assets by voice."
                        onSubmit={handleLiveDirector}
                    />
                ) : displayStage === 'production' ? (
                    <ProductionTimelineEditor
                        fragments={productionTimeline}
                        clips={project.timeline}
                        musicUrl={project.music_url}
                        playheadSeconds={playheadSeconds}
                        totalDuration={productionDuration}
                        isPlaying={isTimelinePlaying}
                        isEditable={!isBusy}
                        selectedFragmentId={selectedProductionFragmentId}
                        selectedTrack={selectedProductionTrack}
                        canSplitSelected={canSplitSelected}
                        canToggleSelectedAudio={canToggleSelectedAudio}
                        selectedFragmentAudioEnabled={selectedProductionFragment?.audio_enabled ?? true}
                        onSelectFragment={handleSelectProductionFragment}
                        onSeek={handleProductionSeek}
                        onTogglePlay={toggleTimelinePlayback}
                        onJumpPrevious={handleJumpToPreviousEdit}
                        onJumpNext={handleJumpToNextEdit}
                        onSplitSelected={handleSplitProductionFragment}
                        onToggleSelectedAudio={handleToggleSelectedProductionAudio}
                        onMoveVideoFragment={handleMoveVideoProductionFragment}
                        onMoveMusicFragment={handleMoveMusicProductionFragment}
                    />
                ) : displayStage === 'lyria_prompting' ? (
                    <React.Fragment>
                        <audio
                            key={project.music_url ?? "no-music-preview"}
                            ref={musicStageAudioRef}
                            src={project.music_url ? toBackendAssetUrl(project.music_url) : undefined}
                            preload="metadata"
                            onLoadedMetadata={syncMusicStagePlaybackState}
                            onDurationChange={syncMusicStagePlaybackState}
                            onTimeUpdate={syncMusicStagePlaybackState}
                            onPlay={syncMusicStagePlaybackState}
                            onPause={syncMusicStagePlaybackState}
                            onEnded={syncMusicStagePlaybackState}
                        />
                        <div className="h-10 border-b border-surface-border flex items-center justify-between px-4 bg-surface/50">
                            <div className="flex space-x-2">
                                <button
                                    title="Jump Back 5 Seconds"
                                    disabled={!canControlMusicStagePreview}
                                    onClick={() => handleJumpMusicStagePlayback(-5)}
                                    className={`p-1.5 rounded ${canControlMusicStagePreview ? "hover:bg-surface-hover text-white/70" : "text-white/25 cursor-not-allowed"}`}
                                >
                                    <SkipBack className="w-4 h-4" />
                                </button>
                                <button
                                    title={isMusicStagePlaying ? "Pause Generated Song" : "Play Generated Song"}
                                    disabled={!canControlMusicStagePreview}
                                    onClick={() => void handleToggleMusicStagePlayback()}
                                    className={`p-1.5 rounded ${canControlMusicStagePreview ? "bg-primary text-white" : "bg-surface-border text-white/25 cursor-not-allowed"}`}
                                >
                                    {isMusicStagePlaying ? <Pause className="w-4 h-4" fill="currentColor" /> : <Play className="w-4 h-4" fill="currentColor" />}
                                </button>
                                <button
                                    title="Jump Forward 5 Seconds"
                                    disabled={!canControlMusicStagePreview}
                                    onClick={() => handleJumpMusicStagePlayback(5)}
                                    className={`p-1.5 rounded ${canControlMusicStagePreview ? "hover:bg-surface-hover text-white/70" : "text-white/25 cursor-not-allowed"}`}
                                >
                                    <SkipForward className="w-4 h-4" />
                                </button>
                            </div>
                            <div className="text-sm text-surface-border font-mono">
                                {formatTransportTime(musicStageCurrentTime)} / {formatTransportTime(musicStageDuration)}
                            </div>
                        </div>

                        <div className="flex-1 bg-background/50 overflow-hidden p-5">
                            <div className="h-full rounded-2xl border border-surface-border bg-black/20 p-5 flex flex-col gap-5">
                                <div className="flex items-start justify-between gap-4">
                                    <div>
                                        <div className="text-[11px] uppercase tracking-[0.22em] text-cyan-300/80">
                                            {usesManualMusicImport ? "Imported Song" : "Generated Song"}
                                        </div>
                                        <div className="text-lg font-semibold text-white/90 mt-1">
                                            {usesManualMusicImport
                                                ? (project.music_url ? "The imported song is ready to audition." : "No imported song is attached yet.")
                                                : hasCurrentAutomaticMusicTrack
                                                    ? "The generated song is ready to audition."
                                                    : project.music_url
                                                        ? "The attached song needs regeneration."
                                                        : "No generated song is attached yet."}
                                        </div>
                                        <p className="text-xs text-surface-border mt-1">
                                            {usesManualMusicImport
                                                ? (project.music_url
                                                    ? "Use these controls to audition the current full song before you confirm the timeline."
                                                    : "Copy the prompt package, render the song in your external tool, then import the exported audio.")
                                                : hasCurrentAutomaticMusicTrack
                                                    ? "Use these controls to audition the current full song before you confirm the timeline."
                                                    : project.music_url
                                                        ? "The song no longer matches the current settings. Regenerate it before you continue to Planning."
                                                        : "Click `Generate Song` after editing the prompts to produce the full song."}
                                        </p>
                                    </div>
                                    {project.music_url && (
                                        <a
                                            href={toBackendAssetUrl(project.music_url)}
                                            download
                                            target="_blank"
                                            rel="noreferrer"
                                            className="inline-flex items-center justify-center rounded-lg border border-cyan-500/30 bg-cyan-500/10 px-3 py-2 text-xs font-medium text-cyan-200 transition-colors hover:bg-cyan-500/20"
                                        >
                                            Export Song
                                        </a>
                                    )}
                                </div>

                                <div className="space-y-3">
                                    <input
                                        type="range"
                                        min={0}
                                        max={musicStageDuration > 0 ? musicStageDuration : 1}
                                        step={0.1}
                                        value={Math.min(musicStageCurrentTime, musicStageDuration || 0)}
                                        disabled={!canControlMusicStagePreview || musicStageDuration <= 0}
                                        onChange={(e) => handleSeekMusicStagePlayback(Number(e.target.value))}
                                        className="w-full accent-cyan-400 disabled:opacity-40"
                                    />
                                    <div className="relative h-28 rounded-xl border border-cyan-500/15 bg-[linear-gradient(180deg,rgba(10,16,30,0.92),rgba(8,12,20,0.72))] overflow-hidden">
                                        <div className="absolute inset-0 opacity-50" style={{ backgroundImage: "linear-gradient(90deg, rgba(34,211,238,0.18) 0 3%, transparent 3% 6%, rgba(96,165,250,0.12) 6% 9%, transparent 9% 12%)", backgroundSize: "48px 100%" }} />
                                        <div
                                            className="absolute inset-y-0 left-0 bg-gradient-to-r from-cyan-400/18 via-cyan-300/28 to-transparent"
                                            style={{ width: `${musicStageDuration > 0 ? (musicStageCurrentTime / musicStageDuration) * 100 : 0}%` }}
                                        />
                                        <div className="absolute inset-y-0 w-px bg-cyan-200/90 shadow-[0_0_12px_rgba(103,232,249,0.85)]" style={{ left: `${musicStageDuration > 0 ? (musicStageCurrentTime / musicStageDuration) * 100 : 0}%` }} />
                                        <div className="absolute inset-0 flex items-end justify-between p-4 text-[11px] text-cyan-100/70">
                                            <span>{project.name}</span>
                                            <span>{project.style_prompt?.trim() || "Generated Song"}</span>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </React.Fragment>
                ) : supportsFooterStagePlayback ? (
                    <React.Fragment>
                        {project.music_url && (
                            <audio
                                ref={footerStageAudioRef}
                                key={`footer-stage-audio:${project.music_url}`}
                                src={getFooterStageAudioSourceUrl()}
                                preload="auto"
                                className="hidden"
                            />
                        )}
                        <div className="h-10 border-b border-surface-border flex items-center justify-between px-4 bg-surface/50">
                            <div className="flex items-center gap-2">
                                <button
                                    title="Jump Back 5 Seconds"
                                    disabled={!canControlFooterStagePlayback}
                                    onClick={() => handleJumpFooterStagePlayback(-5)}
                                    className={`p-1.5 rounded ${canControlFooterStagePlayback ? "hover:bg-surface-hover text-white/70" : "text-white/25 cursor-not-allowed"}`}
                                >
                                    <SkipBack className="w-4 h-4" />
                                </button>
                                <button
                                    title={isStagePlaybackPlaying ? "Pause playback" : "Play playback"}
                                    disabled={!canControlFooterStagePlayback}
                                    onClick={() => void handleToggleFooterStagePlayback()}
                                    className={`p-1.5 rounded ${canControlFooterStagePlayback ? "bg-primary text-white" : "bg-surface-border text-white/25 cursor-not-allowed"}`}
                                >
                                    {isStagePlaybackPlaying ? <Pause className="w-4 h-4" fill="currentColor" /> : <Play className="w-4 h-4" fill="currentColor" />}
                                </button>
                                <button
                                    title="Jump Forward 5 Seconds"
                                    disabled={!canControlFooterStagePlayback}
                                    onClick={() => handleJumpFooterStagePlayback(5)}
                                    className={`p-1.5 rounded ${canControlFooterStagePlayback ? "hover:bg-surface-hover text-white/70" : "text-white/25 cursor-not-allowed"}`}
                                >
                                    <SkipForward className="w-4 h-4" />
                                </button>
                                <div>
                                    <div className="text-[11px] uppercase tracking-[0.18em] text-cyan-300/80">
                                        {displayStage === "input"
                                            ? "Input Playback"
                                            : displayStage === "planning"
                                                ? "Planning Playback"
                                                : displayStage === "storyboarding"
                                                    ? "Storyboard Playback"
                                                    : "Filming Playback"}
                                    </div>
                                    <div className="text-xs text-surface-border">
                                        {project.music_url
                                            ? `Music enters at ${planningMusicStartSeconds.toFixed(1)}s and the bottom timeline stays in sync.`
                                            : "Attach or generate music to enable playback."}
                                    </div>
                                </div>
                            </div>
                            <div className="text-sm text-surface-border font-mono">
                                {formatTransportTime(stagePlaybackSeconds)} / {formatTransportTime(stageTimelineDuration)}
                            </div>
                        </div>

                        <div className="flex-1 bg-background/50 overflow-hidden p-4">
                            {hasFooterStageContent ? (
                                <div className="h-full overflow-auto">
                                    <StageTimelineOverview
                                        clips={project.timeline}
                                        musicStartSeconds={planningMusicStartSeconds}
                                        musicDurationSeconds={productionMusicDuration > 0 ? productionMusicDuration : undefined}
                                        showMusic={!!project.music_url}
                                        playheadSeconds={stagePlaybackSeconds}
                                        onSeek={hasFooterStageContent ? handleFooterStageSeek : undefined}
                                        label={displayStage === "input"
                                            ? "Input Timeline"
                                            : displayStage === "planning"
                                                ? "Planning Timeline"
                                                : displayStage === "storyboarding"
                                                    ? "Storyboard Timeline"
                                                    : "Filming Timeline"}
                                    />
                                </div>
                            ) : null}
                        </div>
                    </React.Fragment>
                ) : (
                    <React.Fragment>
                        <div className="h-10 border-b border-surface-border flex items-center justify-between px-4 bg-surface/50">
                            <div>
                                <div className="text-[11px] uppercase tracking-[0.18em] text-cyan-300/80">
                                    {displayStage === "filming"
                                        ? "Filming Timeline"
                                        : "Timeline Overview"}
                                </div>
                                <div className="text-xs text-surface-border">
                                    The bottom timeline reflects shot timing and where the music enters the cut.
                                </div>
                            </div>
                            <div className="text-sm text-surface-border font-mono">
                                {formatTransportTime(stageTimelineDuration)}
                            </div>
                        </div>

                        <div className="flex-1 bg-background/50 overflow-hidden p-4">
                            <div className="h-full overflow-auto">
                                <StageTimelineOverview
                                    clips={project.timeline}
                                    musicStartSeconds={planningMusicStartSeconds}
                                    musicDurationSeconds={productionMusicDuration > 0 ? productionMusicDuration : undefined}
                                    showMusic={!!project.music_url}
                                    label={displayStage === "filming" ? "Filming Timeline" : "Timeline Overview"}
                                />
                            </div>
                        </div>
                    </React.Fragment>
                )}
            </footer>
            )}

            <input
                type="file"
                id="asset-viewer-audio-upload"
                className="hidden"
                accept="audio/*"
                onChange={(e) => handleFileUpload(e, "audio")}
            />
            <input
                type="file"
                id="asset-viewer-image-upload"
                className="hidden"
                accept="image/*"
                onChange={(e) => handleFileUpload(e, "image")}
            />
            <input
                type="file"
                id="asset-viewer-video-upload"
                className="hidden"
                accept="video/*"
                onChange={(e) => handleFileUpload(e, "video")}
            />
            <input
                type="file"
                id="asset-viewer-document-upload"
                className="hidden"
                accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                onChange={(e) => handleFileUpload(e, "document")}
            />

            {/* Settings Modal */}
            <SettingsModal
                isOpen={isSettingsOpen}
                onClose={() => setIsSettingsOpen(false)}
            />

            {/* Full-Screen Confirmation Dialog overlay */}
            {
                confirmDialog && (
                    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
                        <div className="bg-surface border border-surface-border p-6 rounded-xl w-full max-w-sm shadow-2xl relative">
                            <button onClick={() => setConfirmDialog(null)} title="Close" aria-label="Close dialog" className="absolute top-4 right-4 text-white/40 hover:text-white/90">
                                <X className="w-5 h-5" />
                            </button>
                            <h2 className="text-xl font-bold mb-4 text-white/90">{confirmDialog.title}</h2>
                            <div className="text-sm text-surface-border mb-6 space-y-2 whitespace-pre-wrap">
                                {confirmDialog.message}
                            </div>
                            <div className="flex justify-end gap-3">
                                <button
                                    onClick={() => setConfirmDialog(null)}
                                    className="px-4 py-2 rounded-lg text-sm text-surface-border hover:bg-surface-hover transition-colors"
                                >
                                    Cancel
                                </button>
                                <button
                                    onClick={confirmDialog.onConfirm}
                                    className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${(confirmDialog as any).danger ? "bg-rose-500 hover:bg-rose-600 text-white" : "bg-primary hover:bg-primary-hover text-white"}`}
                                >
                                    Confirm
                                </button>
                            </div>
                        </div>
                    </div>
                )
            }
            {zoomedStoryboardImage && (
                <div
                    className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm px-6 py-10"
                    onClick={() => setZoomedStoryboardImage(null)}
                >
                    <div
                        className="flex max-h-full max-w-6xl w-full flex-col items-center gap-3"
                        onClick={(e) => e.stopPropagation()}
                    >
                        <div className="text-sm font-medium text-white/90">
                            {zoomedStoryboardImage.label}
                        </div>
                        <img
                            src={zoomedStoryboardImage.url}
                            alt={zoomedStoryboardImage.label}
                            className="max-h-[82vh] w-auto max-w-full rounded-2xl border border-surface-border shadow-2xl"
                            draggable={false}
                        />
                    </div>
                </div>
            )}
            {!isAssetViewerOpen && (
                <LiveDirectorPanel
                    currentStage={displayStage}
                    focusLabel={liveDirectorFocusLabel}
                    turns={project.director_log ?? []}
                    isBusy={isBusy}
                    isProcessing={isDirectorProcessing}
                    onSubmit={handleLiveDirector}
                />
            )}
        </div >
    );
}
