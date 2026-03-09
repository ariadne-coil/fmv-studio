"use client";

import React, { useState, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { GlassCard } from "@/components/ui/GlassCard";
import LiveDirectorPanel from "@/components/ui/LiveDirectorPanel";
import ProductionTimelineEditor from "@/components/ui/ProductionTimelineEditor";
import SettingsModal from "@/components/ui/SettingsModal";
import ShotListEditor from "@/components/ui/ShotListEditor";
import { Loader2, Music, ImageIcon, Play, Pause, SkipBack, SkipForward, Wand2, X, AlertCircle, Save, Video, Settings, ListPlus, Maximize2, GripVertical, Home, FileText } from 'lucide-react';
import { api, getMusicProviderOption, getStoredModels, getStoredPreferences, isManualImportMusicProvider, LiveDirectorResponse, normalizeMusicProviderId, ProductionTimelineFragment, ProjectRunStatus, ProjectState, toBackendAssetUrl, VideoClip } from "@/lib/api";

const MIN_PRODUCTION_FRAGMENT_DURATION = 0.25;
const MIN_MUSIC_FRAGMENT_DURATION = 0.25;
const DEFAULT_MUSIC_MIN_DURATION_SECONDS = 90;
const DEFAULT_MUSIC_MAX_DURATION_SECONDS = 240;

function buildDefaultProductionTimeline(
    clips: VideoClip[],
    options?: {
        includeMusic?: boolean;
        musicDuration?: number | null;
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

    const musicDuration = Number.isFinite(options.musicDuration)
        ? Number(options.musicDuration)
        : currentTime;
    const defaultMusicDuration = Math.max(
        MIN_MUSIC_FRAGMENT_DURATION,
        Math.min(currentTime, musicDuration || currentTime),
    );

    return [
        ...videoFragments,
        {
            id: "music_frag_0",
            track_type: "music",
            source_clip_id: null,
            timeline_start: 0,
            source_start: 0,
            duration: defaultMusicDuration,
            audio_enabled: true,
        },
    ];
}

function normalizeProductionTimeline(
    fragments: ProductionTimelineFragment[],
    totalVideoDuration: number,
    musicDuration?: number | null,
): ProductionTimelineFragment[] {
    let currentVideoTime = 0;
    const normalizedVideo = fragments
        .filter((fragment) => (fragment.track_type ?? "video") !== "music" && fragment.duration > 0)
        .sort((left, right) => left.timeline_start - right.timeline_start)
        .map((fragment) => {
            const normalized = {
                ...fragment,
                track_type: "video" as const,
                source_start: Math.max(0, Number(fragment.source_start.toFixed(3))),
                duration: Math.max(MIN_PRODUCTION_FRAGMENT_DURATION, Number(fragment.duration.toFixed(3))),
                timeline_start: Number(currentVideoTime.toFixed(3)),
                audio_enabled: fragment.audio_enabled ?? true,
            };
            currentVideoTime += normalized.duration;
            return normalized;
        });

    const normalizedMusic: ProductionTimelineFragment[] = [];
    let previousMusicEnd = 0;
    for (const fragment of fragments
        .filter((item) => (item.track_type ?? "video") === "music" && item.duration > 0)
        .sort((left, right) => left.timeline_start - right.timeline_start)) {
        let timelineStart = Math.max(0, Number(fragment.timeline_start.toFixed(3)));
        timelineStart = Math.max(timelineStart, Number(previousMusicEnd.toFixed(3)));
        if (totalVideoDuration > 0) {
            timelineStart = Math.min(timelineStart, Math.max(0, totalVideoDuration - MIN_MUSIC_FRAGMENT_DURATION));
        }

        let sourceStart = Math.max(0, Number(fragment.source_start.toFixed(3)));
        if (Number.isFinite(musicDuration) && (musicDuration ?? 0) > 0) {
            sourceStart = Math.min(sourceStart, Math.max(0, (musicDuration ?? 0) - MIN_MUSIC_FRAGMENT_DURATION));
        }

        let duration = Math.max(MIN_MUSIC_FRAGMENT_DURATION, Number(fragment.duration.toFixed(3)));
        if (totalVideoDuration > 0) {
            duration = Math.min(duration, Math.max(MIN_MUSIC_FRAGMENT_DURATION, totalVideoDuration - timelineStart));
        }
        if (Number.isFinite(musicDuration) && (musicDuration ?? 0) > 0) {
            duration = Math.min(duration, Math.max(MIN_MUSIC_FRAGMENT_DURATION, (musicDuration ?? 0) - sourceStart));
        }
        if (duration <= 0) continue;

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

function formatTransportTime(seconds: number): string {
    if (!Number.isFinite(seconds) || seconds <= 0) return "00:00";
    const totalSeconds = Math.max(0, Math.floor(seconds));
    const minutes = Math.floor(totalSeconds / 60);
    const remainingSeconds = totalSeconds % 60;
    return `${String(minutes).padStart(2, "0")}:${String(remainingSeconds).padStart(2, "0")}`;
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
    const [isRegeneratingMusic, setIsRegeneratingMusic] = useState(false);
    const [isDirectorProcessing, setIsDirectorProcessing] = useState(false);
    const [zoomedStoryboardImage, setZoomedStoryboardImage] = useState<{ url: string; label: string } | null>(null);
    const [musicStageCurrentTime, setMusicStageCurrentTime] = useState(0);
    const [musicStageDuration, setMusicStageDuration] = useState(0);
    const [isMusicStagePlaying, setIsMusicStagePlaying] = useState(false);
    const [productionMusicDuration, setProductionMusicDuration] = useState(0);

    // Stage-specific UI states
    const [selectedPlanningShotId, setSelectedPlanningShotId] = useState<string | null>(null);
    const [selectedFilmingClipId, setSelectedFilmingClipId] = useState<string | null>(null);
    const [selectedProductionFragmentId, setSelectedProductionFragmentId] = useState<string | null>(null);
    const [selectedProductionTrack, setSelectedProductionTrack] = useState<ProductionTrackType>("video");
    const [playheadSeconds, setPlayheadSeconds] = useState(0);
    const [isTimelinePlaying, setIsTimelinePlaying] = useState(false);

    // Storyboard Drag & Drop refs
    const dragSrc = useRef<number | null>(null);
    const dragOver = useRef<number | null>(null);
    const musicStageAudioRef = useRef<HTMLAudioElement | null>(null);
    const stageBriefAudioRef = useRef<HTMLAudioElement | null>(null);
    const knownStageBriefsRef = useRef<Record<string, string>>({});
    const pendingStageBriefAutoplayRef = useRef<string | null>(null);
    const stageBriefsHydratedRef = useRef(false);
    const productionVideoRef = useRef<HTMLVideoElement | null>(null);
    const productionMusicRef = useRef<HTMLAudioElement | null>(null);
    const timelineAnimationFrameRef = useRef<number | null>(null);
    const timelineLastFrameRef = useRef<number | null>(null);
    const playheadRef = useRef(0);

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

    const activeStageIndex = project ? (stageMap[project.current_stage] ?? 0) : 0;
    const displayStage = viewedStage || (project?.current_stage ?? 'input');
    const displayStageIndex = stageMap[displayStage] ?? 0;
    const isCurrentDisplayedStage = displayStage === (project?.current_stage ?? 'input');
    const defaultVideoProgramDuration = project
        ? project.timeline.reduce((sum, clip) => sum + clip.duration, 0)
        : 0;
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
                        }).filter((fragment) => fragment.track_type === "music"),
                    ]
                    : project.production_timeline
                : buildDefaultProductionTimeline(project.timeline, {
                    includeMusic: !!project.music_url,
                    musicDuration: productionMusicDuration || undefined,
                }),
            defaultVideoProgramDuration,
            productionMusicDuration || undefined,
        )
        : [];
    const liveDirectorFocusLabel = (() => {
        if (!project) return null;
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
    const activeProductionFragment = getTrackFragmentAtTime(productionTimeline, playheadSeconds, "video");
    const activeProductionClip = project && activeProductionFragment
        ? project.timeline.find((clip) => clip.id === activeProductionFragment.source_clip_id) ?? null
        : null;
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
        if (displayStage !== 'filming' || !project) return;

        const selectedClip = selectedFilmingClipId
            ? project.timeline.find((clip) => clip.id === selectedFilmingClipId) ?? null
            : null;
        if (selectedClip?.video_url) return;

        setSelectedFilmingClipId(
            project.timeline.find((clip) => !!clip.video_url)?.id
            ?? selectedClip?.id
            ?? project.timeline[0]?.id
            ?? null
        );
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

    const syncProductionMedia = (seconds: number, autoplay: boolean) => {
        if (!project || !isProductionDisplay) return;

        const clampedSeconds = Math.max(0, Math.min(seconds, productionDuration));
        const fragment = getTrackFragmentAtTime(productionTimeline, clampedSeconds, "video");
        const musicFragment = getTrackFragmentAtTime(productionTimeline, clampedSeconds, "music");
        const clip = fragment
            ? project.timeline.find((timelineClip) => timelineClip.id === fragment.source_clip_id) ?? null
            : null;

        const musicElement = productionMusicRef.current;
        if (musicElement && project.music_url) {
            if (musicFragment) {
                const musicSeconds = musicFragment.source_start + Math.max(0, clampedSeconds - musicFragment.timeline_start);
                const drift = Math.abs(musicElement.currentTime - musicSeconds);
                if (drift > 0.25) {
                    musicElement.currentTime = musicSeconds;
                }
                if (autoplay) {
                    void musicElement.play().catch(() => {});
                } else {
                    musicElement.pause();
                }
            } else {
                musicElement.pause();
            }
        }

        const videoElement = productionVideoRef.current;
        if (!videoElement || !fragment || !clip?.video_url) {
            videoElement?.pause();
            return;
        }
        videoElement.muted = !(fragment.audio_enabled ?? true);

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

        if (videoElement.readyState >= 1) {
            applyVideoSync();
            return;
        }

        const handleLoadedMetadata = () => applyVideoSync();
        videoElement.addEventListener("loadedmetadata", handleLoadedMetadata, { once: true });
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
        const nextVideoDuration = fragments
            .filter((fragment) => (fragment.track_type ?? "video") !== "music")
            .reduce((sum, fragment) => sum + fragment.duration, 0);
        const normalizedTimeline = normalizeProductionTimeline(
            fragments,
            nextVideoDuration,
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
            timelineLastFrameRef.current = null;
            setIsTimelinePlaying(false);
            productionVideoRef.current?.pause();
            productionMusicRef.current?.pause();
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
            productionVideoRef.current?.pause();
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
            alert("Failed to upload file to backend.");
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
            const updatedProjectState = {
                ...project,
                final_video_url: undefined,
                last_error: undefined,
                timeline: project.timeline.map(clip =>
                    clip.id === clipId
                        ? {
                            ...clip,
                            image_url: uploaded.url,
                            image_approved: true,
                            image_critiques: [
                                ...clip.image_critiques,
                                `Manual storyboard frame uploaded: ${uploaded.name}`
                            ],
                            video_url: undefined,
                            video_critiques: [],
                            video_approved: null,
                        }
                        : clip
                ),
            };

            const serverProject = await api.updateProject(project.project_id, updatedProjectState);
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

    const handleImageApproval = async (clipId: string, approved: boolean) => {
        if (!project) return;
        const newTimeline = project.timeline.map(clip =>
            clip.id === clipId ? { ...clip, image_approved: approved } : clip
        );
        const updatedProjectState = { ...project, timeline: newTimeline };

        try {
            const serverProject = await api.updateProject(project.project_id, updatedProjectState);
            setProject(serverProject);
        } catch (e) {
            console.error(e);
        }
    };

    const handleVideoApproval = async (clipId: string, approved: boolean) => {
        if (!project) return;
        const targetClip = project.timeline.find(clip => clip.id === clipId);
        if (approved && !targetClip?.video_url) return;
        const newTimeline = project.timeline.map(clip =>
            clip.id === clipId ? { ...clip, video_approved: approved } : clip
        );
        const updatedProjectState = { ...project, timeline: newTimeline };

        try {
            const serverProject = await api.updateProject(project.project_id, updatedProjectState);
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
            const serverProject = await api.updateProject(nextProject.project_id, nextProject);
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
        const response = await fetch(toBackendAssetUrl(pathOrUrl), { cache: "no-store" });
        if (!response.ok) {
            throw new Error(`Failed to download ${pathOrUrl} (${response.status})`);
        }
        return response.blob();
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

    // Map each stage to its predecessor for the "← Back" button
    const PREV_STAGE: Record<string, string> = {
        'lyria_prompting': 'input',
        'planning': 'lyria_prompting',  // back to music prompts, keeps shot list
        'storyboarding': 'planning',
        'filming': 'storyboarding',
        'production': 'filming',
        'completed': 'production',
        'halted_for_review': 'planning',
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
            setViewedStage(null);

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
                                                                    {asset.text_content || "Document uploaded for contextual reference."}
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
                                                                    {asset.text_content || "Supplemental document context"}
                                                                </p>
                                                            )}
                                                            {asset.type === 'image' && (
                                                                <p className="mt-1 text-[11px] leading-relaxed text-white/55">
                                                                    Use labels for named characters, props, creatures, vehicles, or locations so the agent can route this reference automatically.
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
                                <ShotListEditor
                                    clips={project.timeline}
                                    projectId={project.project_id}
                                    expandedShotId={selectedPlanningShotId}
                                    onChange={(clips) => setProject({ ...project, timeline: clips })}
                                    onExpand={(id) => setSelectedPlanningShotId(prev => prev === id ? null : id)}
                                />
                                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5 gap-6 pb-12">
                                    {project.timeline.map((clip, index) => (
                                        <div
                                            key={clip.id}
                                            className={`relative p-3 bg-surface/40 backdrop-blur border rounded-xl cursor-grab active:cursor-grabbing transition-shadow ${selectedPlanningShotId === clip.id
                                                ? 'border-primary ring-2 ring-primary/40 bg-primary/10'
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
                                                    setProject({ ...project, timeline: newClips });
                                                }
                                                dragSrc.current = null;
                                                dragOver.current = null;
                                            }}
                                        >
                                            <div className="flex justify-between items-center mb-2">
                                                <span className="flex items-center text-xs font-semibold text-primary uppercase tracking-wider">
                                                    <GripVertical className="w-3.5 h-3.5 mr-1 opacity-50" />
                                                    Shot {index + 1}
                                                </span>
                                                <span className="text-xs text-surface-border font-mono">{clip.duration.toFixed(1)}s</span>
                                            </div>
                                            {/* Image Display */}
                                            <div className="mb-3 rounded overflow-hidden relative bg-black/50 aspect-video flex items-center justify-center border border-surface-border group">
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
                                                    </>
                                                ) : isStoryboardingRunActive ? (
                                                    <div className="text-white/50 text-xs flex flex-col items-center p-4 text-center">
                                                        <Loader2 className="w-6 h-6 animate-spin mb-2" />
                                                        Generating with NanoBanana...
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
                                                    className="w-full min-h-[60px] bg-background/50 border border-surface-border rounded p-2 text-xs text-white/90 focus:outline-none focus:border-primary/50 resize-y"
                                                    placeholder="Storyboard description..."
                                                    title={`Storyboard description for Clip ${index + 1}`}
                                                />
                                                <input
                                                    type="file"
                                                    id={`storyboard-frame-upload-${clip.id}`}
                                                    className="hidden"
                                                    accept="image/*"
                                                    onChange={(e) => handleStoryboardFrameSwap(clip.id, e)}
                                                />
                                                <button
                                                    onClick={() => document.getElementById(`storyboard-frame-upload-${clip.id}`)?.click()}
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
                                                        onClick={() => handleImageApproval(clip.id, true)}
                                                        className={`flex-1 py-1.5 rounded text-xs font-medium transition-colors ${clip.image_approved === true ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30' : 'bg-surface-border hover:bg-surface-hover text-white/70'}`}
                                                    >
                                                        {clip.image_approved === true ? "Approved" : "Approve"}
                                                    </button>
                                                    <button
                                                        onClick={() => handleImageApproval(clip.id, false)}
                                                        className={`flex-1 py-1.5 rounded text-xs font-medium transition-colors ${clip.image_approved === false ? 'bg-rose-500/20 text-rose-400 border border-rose-500/30' : 'bg-surface-border hover:bg-surface-hover text-white/70'}`}
                                                    >
                                                        {clip.image_approved === false ? "Rejected" : "Reject / Regenerate"}
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
                                    <div className="grid grid-cols-1 gap-4">
                                        {project.timeline.map((clip, index) => (
                                            <GlassCard
                                                key={clip.id}
                                                className={`relative !p-3 cursor-pointer transition-colors ${selectedFilmingClipId === clip.id ? 'ring-2 ring-primary bg-primary/10' : 'hover:bg-surface-hover/50'}`}
                                                onClick={() => setSelectedFilmingClipId(clip.id)}
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
                                                    {clip.video_critiques.length > 0 && (
                                                        <div className="rounded border border-amber-500/20 bg-black/20 p-2 text-[11px] text-amber-200/80">
                                                            {clip.video_critiques[clip.video_critiques.length - 1]}
                                                        </div>
                                                    )}
                                                </div>
                                            </GlassCard>
                                        ))}
                                    </div>
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
                                        <a
                                            href={toBackendAssetUrl(project.final_video_url)}
                                            download
                                            target="_blank"
                                            className="inline-flex items-center justify-center w-full px-4 py-3 bg-primary hover:bg-primary-hover text-white text-sm font-medium rounded-lg transition-colors shadow-lg shadow-primary/20"
                                        >
                                            Download Master Video
                                        </a>
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
                                    onClick={() => handleRevert(PREV_STAGE[displayStage] ?? 'input')}
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
                                    <a
                                        href={toBackendAssetUrl(project.final_video_url)}
                                        download
                                        target="_blank"
                                        rel="noreferrer"
                                        className="inline-flex w-full items-center justify-center rounded-xl border border-emerald-500/30 bg-emerald-500/15 px-4 py-3.5 text-sm font-semibold text-emerald-300 transition-colors hover:bg-emerald-500/25"
                                    >
                                        Export Current Final Clip
                                    </a>
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
                            ) : displayStage === 'filming' && selectedFilmingClipId && project.timeline.find(c => c.id === selectedFilmingClipId)?.video_url ? (
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
                                        {activeProductionClip?.video_url ? (
                                            <video
                                                key={activeProductionFragment?.id ?? "production-monitor"}
                                                ref={productionVideoRef}
                                                src={toBackendAssetUrl(activeProductionClip.video_url)}
                                                className="w-full h-full object-contain"
                                                playsInline
                                            />
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
                                <video
                                    src={toBackendAssetUrl(project.final_video_url)}
                                    className="w-full h-full object-contain"
                                    controls
                                    autoPlay
                                />
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
            </main>

            {/* Bottom Timeline */}
            <footer className="h-80 min-h-72 shrink-0 border-t border-surface-border glass z-20 flex min-h-0 flex-col">
                {displayStage === 'production' ? (
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
                ) : (
                    <React.Fragment>
                        <div className="h-10 border-b border-surface-border flex items-center justify-between px-4 bg-surface/50">
                            <div className="flex space-x-2">
                                <button title="Skip Back" className="p-1.5 rounded hover:bg-surface-hover text-white/70"><SkipBack className="w-4 h-4" /></button>
                                <button title="Play" className="p-1.5 rounded bg-primary text-white"><Play className="w-4 h-4" fill="currentColor" /></button>
                                <button title="Skip Forward" className="p-1.5 rounded hover:bg-surface-hover text-white/70"><SkipForward className="w-4 h-4" /></button>
                            </div>
                            <div className="text-sm text-surface-border font-mono">00:00:00:00</div>
                        </div>

                        <div className="flex-1 bg-background/50 overflow-x-auto overflow-y-hidden p-2 relative">
                            <div className="absolute top-0 bottom-0 left-32 w-px bg-red-500 z-10">
                                <div className="absolute top-0 -translate-x-1/2 w-3 h-3 bg-red-500 rounded-sm" />
                            </div>

                            <div className="flex mt-6 space-x-1 pl-4">
                                <div className="w-24 h-12 bg-surface-border rounded flex items-center justify-center text-xs text-white/50 shrink-0">Track 1</div>
                                {project.timeline.length === 0 ? (
                                    <div className="h-12 border-dashed border border-white/20 rounded w-full flex items-center justify-center text-xs text-white/30">
                                        No clips generated yet. Define a screenplay to populate.
                                    </div>
                                ) : (() => {
                                    const PX_PER_SEC = 40;
                                    return project.timeline.map((clip, i) => (
                                        <div
                                            key={clip.id}
                                            style={{ width: `${Math.max(40, clip.duration * PX_PER_SEC)}px` }}
                                            className={`h-12 rounded border border-white/10 shrink-0 flex flex-col justify-center px-2 text-xs overflow-hidden ${i % 2 === 0 ? 'bg-primary/20' : 'bg-purple-500/20'}`}
                                            title={`Shot ${i + 1} — ${clip.duration.toFixed(1)}s`}
                                        >
                                            <span className="truncate opacity-70">{clip.storyboard_text.substring(0, 24) || `Shot ${i + 1}`}…</span>
                                            <span className="opacity-40 font-mono text-[10px] mt-0.5">{clip.duration.toFixed(1)}s</span>
                                        </div>
                                    ));
                                })()}
                            </div>

                            <div className="flex mt-2 space-x-1 pl-4">
                                <div className="w-24 h-12 bg-surface-border rounded flex items-center justify-center text-xs text-white/50 shrink-0">Audio</div>
                                {(() => {
                                    const PX_PER_SEC = 40;
                                    const totalDuration = project.timeline.reduce((sum, c) => sum + c.duration, 0);
                                    const audioW = Math.max(160, totalDuration * PX_PER_SEC);
                                    return (
                                        <div
                                            style={{ width: `${audioW}px` }}
                                            className="h-12 bg-emerald-500/20 border border-emerald-500/30 rounded shrink-0 flex items-center justify-between px-2 text-xs text-emerald-500/70 group"
                                        >
                                            <span>Music Track {totalDuration > 0 ? `— ${totalDuration.toFixed(1)}s` : ""}</span>
                                            {project.music_url && (
                                                <a
                                                    href={toBackendAssetUrl(project.music_url)}
                                                    download="music_track.mp3"
                                                    target="_blank"
                                                    rel="noreferrer"
                                                    className="p-1 hover:text-emerald-300 transition-colors opacity-0 group-hover:opacity-100"
                                                    title="Save Music Track"
                                                    onClick={(e) => e.stopPropagation()}
                                                >
                                                    <Save className="w-3.5 h-3.5" />
                                                </a>
                                            )}
                                        </div>
                                    );
                                })()}
                            </div>
                        </div>
                    </React.Fragment>
                )}
            </footer>

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
            <LiveDirectorPanel
                currentStage={displayStage}
                focusLabel={liveDirectorFocusLabel}
                turns={project.director_log ?? []}
                isBusy={isBusy}
                isProcessing={isDirectorProcessing}
                onSubmit={handleLiveDirector}
            />
        </div >
    );
}
