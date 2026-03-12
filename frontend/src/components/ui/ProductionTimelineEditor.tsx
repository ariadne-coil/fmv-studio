"use client";

import React from "react";
import { Film, Music2, Pause, Play, Scissors, SkipBack, SkipForward, Volume2, VolumeX } from "lucide-react";
import { ProductionTimelineFragment, VideoClip } from "@/lib/api";

const PX_PER_SEC = 84;
const TRACK_LABEL_WIDTH = 120;
const TIMELINE_END_GUTTER_PX = 96;

function formatTimecode(seconds: number): string {
    const safeSeconds = Math.max(0, seconds);
    const hours = Math.floor(safeSeconds / 3600);
    const minutes = Math.floor((safeSeconds % 3600) / 60);
    const secs = Math.floor(safeSeconds % 60);
    const frames = Math.floor((safeSeconds % 1) * 30);
    return [hours, minutes, secs, frames]
        .map((value) => value.toString().padStart(2, "0"))
        .join(":");
}

type ProductionTimelineEditorProps = {
    fragments: ProductionTimelineFragment[];
    clips: VideoClip[];
    musicUrl?: string;
    playheadSeconds: number;
    totalDuration: number;
    isPlaying: boolean;
    isEditable: boolean;
    selectedFragmentId: string | null;
    selectedTrack: "video" | "audio" | "music";
    canSplitSelected: boolean;
    canToggleSelectedAudio: boolean;
    selectedFragmentAudioEnabled: boolean;
    onSelectFragment: (fragmentId: string, track: "video" | "audio" | "music") => void;
    onSeek: (seconds: number) => void;
    onTogglePlay: () => void;
    onJumpPrevious: () => void;
    onJumpNext: () => void;
    onSplitSelected: () => void;
    onToggleSelectedAudio: () => void;
    onMoveVideoFragment: (draggedFragmentId: string, beforeFragmentId: string | null) => void;
    onMoveMusicFragment: (draggedFragmentId: string, timelineStartSeconds: number) => void;
};

export default function ProductionTimelineEditor({
    fragments,
    clips,
    musicUrl,
    playheadSeconds,
    totalDuration,
    isPlaying,
    isEditable,
    selectedFragmentId,
    selectedTrack,
    canSplitSelected,
    canToggleSelectedAudio,
    selectedFragmentAudioEnabled,
    onSelectFragment,
    onSeek,
    onTogglePlay,
    onJumpPrevious,
    onJumpNext,
    onSplitSelected,
    onToggleSelectedAudio,
    onMoveVideoFragment,
    onMoveMusicFragment,
}: ProductionTimelineEditorProps) {
    const sortedVideoFragments = [...fragments]
        .filter((fragment) => (fragment.track_type ?? "video") !== "music")
        .sort((left, right) => left.timeline_start - right.timeline_start);
    const sortedMusicFragments = [...fragments]
        .filter((fragment) => (fragment.track_type ?? "video") === "music")
        .sort((left, right) => left.timeline_start - right.timeline_start);
    const furthestFragmentEndSeconds = fragments.reduce(
        (maxEnd, fragment) => Math.max(maxEnd, fragment.timeline_start + fragment.duration),
        0
    );
    const effectiveTimelineDuration = Math.max(totalDuration, furthestFragmentEndSeconds);
    const totalWidth = Math.max(
        720,
        Math.ceil(effectiveTimelineDuration * PX_PER_SEC) + TIMELINE_END_GUTTER_PX,
    );

    const clipLookup = Object.fromEntries(
        clips.map((clip, index) => [
            clip.id,
            {
                clip,
                label: `Shot ${index + 1}`,
                title: clip.storyboard_text || `Shot ${index + 1}`,
            },
        ])
    );

    const selectedFragment = [...sortedVideoFragments, ...sortedMusicFragments].find((fragment) => fragment.id === selectedFragmentId) ?? null;

    const handleTrackSeek = (event: React.MouseEvent<HTMLDivElement>) => {
        const rect = event.currentTarget.getBoundingClientRect();
        const offsetX = event.clientX - rect.left;
        onSeek(offsetX / PX_PER_SEC);
    };

    const handleVideoTrackDrop = (event: React.DragEvent<HTMLDivElement>) => {
        event.preventDefault();
        if (!isEditable) return;
        const draggedFragmentId = event.dataTransfer.getData("text/fragment-id");
        if (!draggedFragmentId) return;

        const rect = event.currentTarget.getBoundingClientRect();
        const offsetX = event.clientX - rect.left;

        let beforeFragmentId: string | null = null;
        for (const fragment of sortedVideoFragments) {
            const midpoint = (fragment.timeline_start + fragment.duration / 2) * PX_PER_SEC;
            if (offsetX < midpoint) {
                beforeFragmentId = fragment.id;
                break;
            }
        }

        onMoveVideoFragment(draggedFragmentId, beforeFragmentId);
    };

    const handleVideoFragmentDrop = (
        event: React.DragEvent<HTMLElement>,
        fragmentId: string,
        insertAfter: boolean,
        fallbackNextFragmentId: string | null,
    ) => {
        event.preventDefault();
        event.stopPropagation();
        if (!isEditable) return;
        const draggedFragmentId = event.dataTransfer.getData("text/fragment-id");
        if (!draggedFragmentId) return;

        const targetBeforeFragmentId = insertAfter ? fallbackNextFragmentId : fragmentId;
        if (targetBeforeFragmentId === draggedFragmentId) return;
        onMoveVideoFragment(draggedFragmentId, targetBeforeFragmentId);
    };

    const handleMusicTrackDrop = (event: React.DragEvent<HTMLDivElement>) => {
        event.preventDefault();
        if (!isEditable) return;
        const draggedFragmentId = event.dataTransfer.getData("text/fragment-id");
        if (!draggedFragmentId) return;

        const rect = event.currentTarget.getBoundingClientRect();
        const offsetX = event.clientX - rect.left;
        onMoveMusicFragment(draggedFragmentId, Math.max(0, offsetX / PX_PER_SEC));
    };

    const handleMusicFragmentDrop = (event: React.DragEvent<HTMLElement>) => {
        event.preventDefault();
        event.stopPropagation();
        if (!isEditable) return;
        const draggedFragmentId = event.dataTransfer.getData("text/fragment-id");
        if (!draggedFragmentId) return;

        const lane = (event.currentTarget.closest("[data-track-role='music-lane']") as HTMLDivElement | null) ?? null;
        const rect = lane?.getBoundingClientRect() ?? event.currentTarget.getBoundingClientRect();
        const offsetX = event.clientX - rect.left;
        onMoveMusicFragment(draggedFragmentId, Math.max(0, offsetX / PX_PER_SEC));
    };

    return (
        <div className="h-full flex flex-col">
            <div className="h-10 border-b border-surface-border flex items-center justify-between px-3 bg-surface/60">
                <div className="flex items-center gap-2">
                    <button
                        title="Previous edit"
                        onClick={onJumpPrevious}
                        className="p-1.5 rounded hover:bg-surface-hover text-white/70 transition-colors"
                    >
                        <SkipBack className="w-4 h-4" />
                    </button>
                    <button
                        title={isPlaying ? "Pause timeline" : "Play timeline"}
                        onClick={onTogglePlay}
                        className="p-1.5 rounded bg-primary text-white hover:bg-primary-hover transition-colors"
                    >
                        {isPlaying ? <Pause className="w-4 h-4" fill="currentColor" /> : <Play className="w-4 h-4" fill="currentColor" />}
                    </button>
                    <button
                        title="Next edit"
                        onClick={onJumpNext}
                        className="p-1.5 rounded hover:bg-surface-hover text-white/70 transition-colors"
                    >
                        <SkipForward className="w-4 h-4" />
                    </button>
                    <div className="w-px h-6 bg-surface-border mx-1" />
                    <button
                        title="Split selected fragment at playhead"
                        onClick={onSplitSelected}
                        disabled={!isEditable || !canSplitSelected}
                        className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded text-[11px] font-medium transition-colors border ${isEditable && canSplitSelected ? "border-amber-500/40 text-amber-300 hover:bg-amber-500/10" : "border-surface-border text-white/30 cursor-not-allowed"}`}
                    >
                        <Scissors className="w-3 h-3" />
                        Split At Playhead
                    </button>
                    <button
                        title={selectedFragmentAudioEnabled ? "Delete source audio from selected A1 fragment" : "Restore source audio to selected A1 fragment"}
                        onClick={onToggleSelectedAudio}
                        disabled={!isEditable || !canToggleSelectedAudio}
                        className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded text-[11px] font-medium transition-colors border ${isEditable && canToggleSelectedAudio
                            ? selectedFragmentAudioEnabled
                                ? "border-rose-500/40 text-rose-300 hover:bg-rose-500/10"
                                : "border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/10"
                            : "border-surface-border text-white/30 cursor-not-allowed"
                            }`}
                    >
                        {selectedFragmentAudioEnabled ? <VolumeX className="w-3 h-3" /> : <Volume2 className="w-3 h-3" />}
                        {selectedFragmentAudioEnabled ? "Delete Audio" : "Restore Audio"}
                    </button>
                </div>

                <div className="text-xs text-surface-border font-mono">{formatTimecode(playheadSeconds)}</div>
            </div>

            <div className="px-3 py-1.5 border-b border-surface-border bg-background/60 flex items-center justify-between gap-3 text-[11px] leading-tight">
                <div className="text-surface-border">
                    {isEditable
                        ? "Drag `V1` to reorder picture, drag `M1` to reposition music, split either track at the playhead, then click `A1` to delete or restore source audio on any fragment."
                        : "Review mode only. Rewind to Production to split or reorder this edit."}
                </div>
                {selectedFragment ? (
                    <div className="text-white/80 font-mono shrink-0 text-[11px]">
                        {(selectedFragment.track_type ?? "video") === "music"
                            ? `Music Segment | M1 | In ${selectedFragment.source_start.toFixed(1)}s | Dur ${selectedFragment.duration.toFixed(1)}s`
                            : `${clipLookup[selectedFragment.source_clip_id ?? ""]?.label ?? selectedFragment.source_clip_id} | ${selectedTrack === "audio" ? "A1" : "V1"} | In ${selectedFragment.source_start.toFixed(1)}s | Dur ${selectedFragment.duration.toFixed(1)}s`}
                    </div>
                ) : (
                    <div className="text-surface-border shrink-0 text-[11px]">No fragment selected</div>
                )}
            </div>

            <div className="flex-1 overflow-x-auto overflow-y-hidden bg-background/70">
                <div className="min-h-full min-w-full w-max p-3">
                    <div className="flex">
                        <div className="w-[120px] shrink-0" />
                        <div
                            className="relative h-6 cursor-pointer"
                            style={{ width: `${totalWidth}px` }}
                            onClick={handleTrackSeek}
                            title="Click to seek"
                        >
                            {Array.from({ length: Math.max(1, Math.ceil(effectiveTimelineDuration / 2) + 1) }).map((_, index) => {
                                const second = index * 2;
                                return (
                                    <div
                                        key={second}
                                        className="absolute top-0 bottom-0 border-l border-white/10 text-[9px] text-surface-border"
                                        style={{ left: `${second * PX_PER_SEC}px` }}
                                    >
                                        <span className="absolute top-0 left-1.5">{formatTimecode(second).slice(0, 8)}</span>
                                    </div>
                                );
                            })}
                        </div>
                    </div>

                    <div className="relative mt-1.5 space-y-2">
                        <div
                            className="absolute top-0 bottom-0 z-20 pointer-events-none"
                            style={{ left: `${TRACK_LABEL_WIDTH + playheadSeconds * PX_PER_SEC}px` }}
                        >
                            <div className="absolute top-0 -translate-x-1/2 w-2.5 h-2.5 bg-rose-500 rounded-sm" />
                            <div className="absolute top-2.5 bottom-0 left-1/2 -translate-x-1/2 w-px bg-rose-500" />
                        </div>

                        <div className="flex items-stretch gap-3">
                            <div className="w-[120px] shrink-0 rounded-xl bg-surface/80 border border-surface-border px-3 py-2 flex flex-col justify-center">
                                <span className="text-[10px] uppercase tracking-[0.2em] text-primary flex items-center gap-1.5">
                                    <Film className="w-3 h-3" />
                                    V1
                                </span>
                                <span className="text-[10px] text-surface-border mt-0.5">Linked picture</span>
                            </div>
                            <div
                                className="relative h-12 rounded-xl border border-surface-border bg-black/25 overflow-hidden"
                                style={{ width: `${totalWidth}px` }}
                                onClick={handleTrackSeek}
                                onDragOver={(event) => event.preventDefault()}
                                onDrop={handleVideoTrackDrop}
                            >
                                {sortedVideoFragments.map((fragment, index) => {
                                    const clipMeta = clipLookup[fragment.source_clip_id ?? ""];
                                    const isSelected = fragment.id === selectedFragmentId && selectedTrack === "video";
                                    return (
                                        <button
                                            key={fragment.id}
                                            draggable={isEditable}
                                            onDragStart={(event) => {
                                                if (!isEditable) {
                                                    event.preventDefault();
                                                    return;
                                                }
                                                event.dataTransfer.effectAllowed = "move";
                                                event.dataTransfer.setData("text/fragment-id", fragment.id);
                                            }}
                                            onDragOver={(event) => {
                                                event.preventDefault();
                                                event.stopPropagation();
                                            }}
                                            onDrop={(event) => {
                                                const rect = event.currentTarget.getBoundingClientRect();
                                                const insertAfter = event.clientX - rect.left >= rect.width / 2;
                                                handleVideoFragmentDrop(
                                                    event,
                                                    fragment.id,
                                                    insertAfter,
                                                    sortedVideoFragments[index + 1]?.id ?? null,
                                                );
                                            }}
                                            onClick={(event) => {
                                                event.stopPropagation();
                                                onSelectFragment(fragment.id, "video");
                                            }}
                                            className={`absolute top-1.5 bottom-1.5 rounded-lg border text-left px-2.5 overflow-hidden transition-all ${isSelected ? "border-primary bg-primary/25 shadow-[0_0_0_1px_rgba(56,189,248,0.4)]" : "border-white/10 bg-sky-500/18 hover:bg-sky-500/24"} ${isEditable ? "cursor-grab active:cursor-grabbing" : "cursor-pointer"}`}
                                            style={{
                                                left: `${fragment.timeline_start * PX_PER_SEC}px`,
                                                width: `${Math.max(40, fragment.duration * PX_PER_SEC)}px`,
                                            }}
                                            title={`${clipMeta?.label ?? fragment.source_clip_id} | In ${fragment.source_start.toFixed(1)}s | Duration ${fragment.duration.toFixed(1)}s`}
                                        >
                                            <div className="h-full flex flex-col justify-between">
                                                <span className="text-[10px] uppercase tracking-[0.16em] text-sky-100/80">{clipMeta?.label ?? fragment.source_clip_id}</span>
                                                <span className="text-[11px] text-white/90 truncate">{clipMeta?.title ?? fragment.source_clip_id}</span>
                                                <span className="text-[9px] text-white/50 font-mono">In {fragment.source_start.toFixed(1)}s • {fragment.duration.toFixed(1)}s</span>
                                            </div>
                                        </button>
                                    );
                                })}
                            </div>
                        </div>

                        <div className="flex items-stretch gap-3">
                            <div className="w-[120px] shrink-0 rounded-xl bg-surface/80 border border-surface-border px-3 py-2 flex flex-col justify-center">
                                <span className="text-[10px] uppercase tracking-[0.2em] text-emerald-300 flex items-center gap-1.5">
                                    <Volume2 className="w-3 h-3" />
                                    A1
                                </span>
                                <span className="text-[10px] text-surface-border mt-0.5">Linked source audio</span>
                            </div>
                            <div
                                className="relative h-12 rounded-xl border border-surface-border bg-black/25 overflow-hidden"
                                style={{ width: `${totalWidth}px` }}
                                onClick={handleTrackSeek}
                            >
                                {sortedVideoFragments.map((fragment) => {
                                    const clipMeta = clipLookup[fragment.source_clip_id ?? ""];
                                    const audioEnabled = fragment.audio_enabled ?? true;
                                    const isSelected = fragment.id === selectedFragmentId && selectedTrack === "audio";
                                    return (
                                        <button
                                            key={`${fragment.id}-audio`}
                                            onClick={(event) => {
                                                event.stopPropagation();
                                                onSelectFragment(fragment.id, "audio");
                                            }}
                                            className={`absolute top-1.5 bottom-1.5 rounded-lg border text-left px-2.5 overflow-hidden transition-all ${isSelected
                                                ? audioEnabled
                                                    ? "border-emerald-300 bg-emerald-500/18 shadow-[0_0_0_1px_rgba(110,231,183,0.35)]"
                                                    : "border-rose-300 bg-rose-500/16 shadow-[0_0_0_1px_rgba(253,164,175,0.3)]"
                                                : audioEnabled
                                                    ? "border-white/10 bg-emerald-500/12 hover:bg-emerald-500/18"
                                                    : "border-white/10 bg-rose-500/12 hover:bg-rose-500/18 opacity-80"
                                                }`}
                                            style={{
                                                left: `${fragment.timeline_start * PX_PER_SEC}px`,
                                                width: `${Math.max(40, fragment.duration * PX_PER_SEC)}px`,
                                                backgroundImage: audioEnabled
                                                    ? "linear-gradient(90deg, rgba(255,255,255,0.12) 0 6%, transparent 6% 12%, rgba(255,255,255,0.08) 12% 18%, transparent 18% 24%)"
                                                    : "repeating-linear-gradient(135deg, rgba(255,255,255,0.12) 0 8px, transparent 8px 16px)",
                                                backgroundSize: "24px 100%",
                                            }}
                                            title={`${clipMeta?.label ?? fragment.source_clip_id} audio ${audioEnabled ? "enabled" : "muted"}`}
                                        >
                                            <div className="h-full flex items-center justify-between gap-2">
                                                <span className={`text-[11px] truncate ${audioEnabled ? "text-emerald-100/90" : "text-rose-100/90"}`}>
                                                    {clipMeta?.label ?? fragment.source_clip_id}
                                                </span>
                                                <span className={`text-[9px] font-mono ${audioEnabled ? "text-emerald-100/60" : "text-rose-100/70"}`}>
                                                    {audioEnabled ? `${fragment.duration.toFixed(1)}s` : "Muted"}
                                                </span>
                                            </div>
                                        </button>
                                    );
                                })}
                            </div>
                        </div>

                        {musicUrl && (
                            <div className="flex items-stretch gap-3">
                                <div className="w-[120px] shrink-0 rounded-xl bg-surface/80 border border-surface-border px-3 py-2 flex flex-col justify-center">
                                    <span className="text-[10px] uppercase tracking-[0.2em] text-fuchsia-300 flex items-center gap-1.5">
                                        <Music2 className="w-3 h-3" />
                                        M1
                                    </span>
                                    <span className="text-[10px] text-surface-border mt-0.5">Music track</span>
                                </div>
                                <div
                                    className="relative h-10 rounded-xl border border-surface-border bg-black/25 overflow-hidden"
                                    style={{ width: `${totalWidth}px` }}
                                    data-track-role="music-lane"
                                    onClick={handleTrackSeek}
                                    onDragOver={(event) => event.preventDefault()}
                                    onDrop={handleMusicTrackDrop}
                                >
                                    {sortedMusicFragments.map((fragment) => {
                                        const isSelected = fragment.id === selectedFragmentId && selectedTrack === "music";
                                        return (
                                            <button
                                                key={fragment.id}
                                                draggable={isEditable}
                                                onDragStart={(event) => {
                                                    if (!isEditable) {
                                                        event.preventDefault();
                                                        return;
                                                    }
                                                    event.dataTransfer.effectAllowed = "move";
                                                    event.dataTransfer.setData("text/fragment-id", fragment.id);
                                                }}
                                                onDragOver={(event) => {
                                                    event.preventDefault();
                                                    event.stopPropagation();
                                                }}
                                                onDrop={handleMusicFragmentDrop}
                                                onClick={(event) => {
                                                    event.stopPropagation();
                                                    onSelectFragment(fragment.id, "music");
                                                }}
                                                className={`absolute inset-y-1.5 rounded-lg border px-2.5 flex items-center justify-between gap-2 text-[11px] transition-all ${isSelected
                                                    ? "border-fuchsia-300 bg-fuchsia-500/22 shadow-[0_0_0_1px_rgba(244,114,182,0.35)]"
                                                    : "border-fuchsia-400/30 bg-fuchsia-500/14 hover:bg-fuchsia-500/20"
                                                    } ${isEditable ? "cursor-grab active:cursor-grabbing" : "cursor-pointer"}`}
                                                style={{
                                                    left: `${fragment.timeline_start * PX_PER_SEC}px`,
                                                    width: `${Math.max(40, fragment.duration * PX_PER_SEC)}px`,
                                                }}
                                                title={`Music segment | In ${fragment.source_start.toFixed(1)}s | Duration ${fragment.duration.toFixed(1)}s`}
                                            >
                                                <span className="truncate text-fuchsia-100/85">Music Segment</span>
                                                <span className="font-mono text-fuchsia-100/65">{fragment.duration.toFixed(1)}s</span>
                                            </button>
                                        );
                                    })}
                                </div>
                            </div>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
}
