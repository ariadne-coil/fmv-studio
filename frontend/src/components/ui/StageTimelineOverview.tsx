"use client";

import React from "react";
import { Music2, Video } from "lucide-react";
import { VideoClip } from "@/lib/api";

const PX_PER_SECOND = 22;
const TIMELINE_MIN_WIDTH = 640;
const TIMELINE_END_GUTTER = 48;
const TRACK_LABEL_WIDTH = 96;

type StageTimelineOverviewProps = {
    clips: VideoClip[];
    musicStartSeconds: number;
    musicDurationSeconds?: number | null;
    showMusic: boolean;
    label?: string;
    playheadSeconds?: number;
    onSeek?: (seconds: number) => void;
};

function formatSeconds(seconds: number): string {
    return `${Math.max(0, seconds).toFixed(1)}s`;
}

export default function StageTimelineOverview({
    clips,
    musicStartSeconds,
    musicDurationSeconds,
    showMusic,
    label = "Timeline Overview",
    playheadSeconds,
    onSeek,
}: StageTimelineOverviewProps) {
    const orderedClips = [...clips].sort((left, right) => left.timeline_start - right.timeline_start);
    const shotDuration = orderedClips.reduce(
        (maxEnd, clip) => Math.max(maxEnd, clip.timeline_start + clip.duration),
        0,
    );
    const normalizedMusicStart = Math.max(0, Number.isFinite(musicStartSeconds) ? musicStartSeconds : 0);
    const musicEnd = showMusic && Number.isFinite(musicDurationSeconds)
        ? normalizedMusicStart + Math.max(0, musicDurationSeconds ?? 0)
        : shotDuration;
    const timelineDuration = Math.max(shotDuration, musicEnd);
    const contentWidth = Math.max(
        TIMELINE_MIN_WIDTH,
        Math.ceil(timelineDuration * PX_PER_SECOND) + TIMELINE_END_GUTTER,
    );
    const laneWidth = Math.max(120, contentWidth - TRACK_LABEL_WIDTH);
    const clampedPlayheadSeconds = Math.max(0, Math.min(playheadSeconds ?? 0, timelineDuration || 0));
    const handleSeek = (event: React.MouseEvent<HTMLDivElement>) => {
        if (!onSeek) return;
        const rect = event.currentTarget.getBoundingClientRect();
        const offsetX = event.clientX - rect.left;
        onSeek(Math.max(0, offsetX / PX_PER_SECOND));
    };

    return (
        <div className="rounded-xl border border-surface-border bg-background/35 p-3">
            <div className="flex items-center justify-between gap-3">
                <div>
                    <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-cyan-300/80">{label}</div>
                    <div className="mt-1 text-xs text-surface-border">
                        Music starts at <span className="font-mono text-white/80">{formatSeconds(normalizedMusicStart)}</span>
                        {showMusic && Number.isFinite(musicDurationSeconds)
                            ? (
                                <>
                                    {" "}and runs for <span className="font-mono text-white/80">{formatSeconds(musicDurationSeconds ?? 0)}</span>.
                                </>
                            )
                            : "."}
                    </div>
                </div>
                <div className="text-[11px] font-mono text-surface-border">
                    Plan Length: {formatSeconds(timelineDuration)}
                </div>
            </div>

            <div className="mt-3 overflow-x-auto">
                <div className="space-y-2" style={{ width: `${contentWidth}px` }}>
                    <div className="flex items-center gap-3">
                        <div className="w-24 shrink-0" />
                        <div
                            className={`relative h-5 border-b border-white/8 ${onSeek ? "cursor-pointer" : ""}`}
                            style={{ width: `${laneWidth}px` }}
                            onClick={handleSeek}
                            title={onSeek ? "Click to seek" : undefined}
                        >
                            {Array.from({ length: Math.max(1, Math.ceil(timelineDuration / 4) + 1) }).map((_, index) => {
                                const second = index * 4;
                                return (
                                    <div
                                        key={second}
                                        className="absolute top-0 bottom-0 border-l border-white/10 text-[10px] text-surface-border"
                                        style={{ left: `${second * PX_PER_SECOND}px` }}
                                    >
                                        <span className="absolute -top-0.5 left-1.5">{second}s</span>
                                    </div>
                                );
                            })}
                            <div
                                className="absolute top-0 bottom-0 w-px bg-rose-400/85 shadow-[0_0_10px_rgba(251,113,133,0.75)]"
                                style={{ left: `${clampedPlayheadSeconds * PX_PER_SECOND}px` }}
                            />
                        </div>
                    </div>

                    <div className="flex items-center gap-3">
                        <div className="w-24 shrink-0 rounded-lg border border-surface-border bg-surface/70 px-3 py-2 text-[10px] uppercase tracking-[0.18em] text-cyan-200">
                            <div className="flex items-center gap-1.5">
                                <Video className="h-3 w-3" />
                                Shots
                            </div>
                        </div>
                        <div
                            className={`relative h-10 flex-1 rounded-xl border border-surface-border bg-black/25 ${onSeek ? "cursor-pointer" : ""}`}
                            style={{ width: `${laneWidth}px` }}
                            onClick={handleSeek}
                            title={onSeek ? "Click to seek" : undefined}
                        >
                            {orderedClips.map((clip, index) => (
                                <div
                                    key={clip.id}
                                    className="absolute inset-y-1 rounded-lg border border-cyan-400/20 bg-cyan-500/15 px-2 py-1 text-[10px] text-cyan-50"
                                    style={{
                                        left: `${clip.timeline_start * PX_PER_SECOND}px`,
                                        width: `${Math.max(40, clip.duration * PX_PER_SECOND)}px`,
                                    }}
                                    title={`Shot ${index + 1} | ${clip.timeline_start.toFixed(1)}s - ${(clip.timeline_start + clip.duration).toFixed(1)}s`}
                                >
                                    <div className="truncate font-semibold">Shot {index + 1}</div>
                                    <div className="truncate text-cyan-100/70">{clip.duration.toFixed(0)}s</div>
                                </div>
                            ))}
                            <div
                                className="absolute top-0 bottom-0 w-px bg-rose-400/85 shadow-[0_0_10px_rgba(251,113,133,0.75)]"
                                style={{ left: `${clampedPlayheadSeconds * PX_PER_SECOND}px` }}
                            />
                        </div>
                    </div>

                    <div className="flex items-center gap-3">
                        <div className="w-24 shrink-0 rounded-lg border border-surface-border bg-surface/70 px-3 py-2 text-[10px] uppercase tracking-[0.18em] text-fuchsia-200">
                            <div className="flex items-center gap-1.5">
                                <Music2 className="h-3 w-3" />
                                Music
                            </div>
                        </div>
                        <div
                            className={`relative h-8 flex-1 rounded-xl border border-surface-border bg-black/25 ${onSeek ? "cursor-pointer" : ""}`}
                            style={{ width: `${laneWidth}px` }}
                            onClick={handleSeek}
                            title={onSeek ? "Click to seek" : undefined}
                        >
                            {showMusic ? (
                                <React.Fragment>
                                    <div
                                        className="absolute top-0 bottom-0 border-l-2 border-fuchsia-300/70"
                                        style={{ left: `${normalizedMusicStart * PX_PER_SECOND}px` }}
                                    />
                                    <div
                                        className="absolute inset-y-1 rounded-lg border border-fuchsia-400/30 bg-fuchsia-500/15 px-2 py-1 text-[10px] text-fuchsia-100"
                                        style={{
                                            left: `${normalizedMusicStart * PX_PER_SECOND}px`,
                                            width: `${Math.max(40, (Math.max(0.25, musicDurationSeconds ?? Math.max(timelineDuration - normalizedMusicStart, 0.25))) * PX_PER_SECOND)}px`,
                                        }}
                                        title={`Music starts at ${normalizedMusicStart.toFixed(1)}s`}
                                    >
                                        <div className="truncate font-semibold">Music In</div>
                                    </div>
                                </React.Fragment>
                            ) : (
                                <div className="absolute inset-0 flex items-center px-3 text-[11px] text-surface-border">
                                    Attach or generate a song to place the music bed on the timeline.
                                </div>
                            )}
                            <div
                                className="absolute top-0 bottom-0 w-px bg-rose-400/85 shadow-[0_0_10px_rgba(251,113,133,0.75)]"
                                style={{ left: `${clampedPlayheadSeconds * PX_PER_SECOND}px` }}
                            />
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}
