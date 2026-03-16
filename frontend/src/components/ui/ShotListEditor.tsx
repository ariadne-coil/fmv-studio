"use client";

import React, { useState, useRef } from "react";
import { VideoClip, getStoredApiKey, getStoredModels } from "@/lib/api";
import { Loader2, Trash2, GripVertical, Wand2, Plus, Maximize2 } from "lucide-react";

interface ShotListEditorProps {
    clips: VideoClip[];
    projectId: string;
    expandedShotId?: string | null;
    onChange: (clips: VideoClip[]) => void;
    onExpand?: (id: string) => void;
    allowedDurations?: readonly (4 | 6 | 8)[];
}

let _idCounter = Date.now();
const VALID_DURATIONS = [4, 6, 8] as const;

function newClipId() { return `clip_custom_${_idCounter++}`; }

function quantizeDuration(duration: number, allowedDurations: readonly (4 | 6 | 8)[] = VALID_DURATIONS): 4 | 6 | 8 {
    const value = Number.isFinite(duration) ? duration : 6;
    const candidates = allowedDurations.length ? allowedDurations : VALID_DURATIONS;
    return candidates.reduce((best, candidate) => {
        const bestDelta = Math.abs(best - value);
        const candidateDelta = Math.abs(candidate - value);
        if (candidateDelta < bestDelta) return candidate;
        if (candidateDelta === bestDelta && candidate > best) return candidate;
        return best;
    }, candidates[candidates.length - 1]);
}

function recalcTimestamps(clips: VideoClip[], allowedDurations: readonly (4 | 6 | 8)[] = VALID_DURATIONS): VideoClip[] {
    let t = 0;
    return clips.map(c => {
        const updated = { ...c, duration: quantizeDuration(c.duration, allowedDurations), timeline_start: t };
        t += updated.duration;
        return updated;
    });
}

export default function ShotListEditor({
    clips,
    projectId,
    expandedShotId,
    onChange,
    onExpand,
    allowedDurations = VALID_DURATIONS,
}: ShotListEditorProps) {
    const [filling, setFilling] = useState<string | null>(null); // clipId being filled
    const dragSrc = useRef<number | null>(null);
    const dragOver = useRef<number | null>(null);

    // ── Drag-and-drop ─────────────────────────────────────────────────────────
    const handleDragStart = (idx: number) => { dragSrc.current = idx; };
    const handleDragEnter = (idx: number) => { dragOver.current = idx; };
    const handleDragEnd = () => {
        if (dragSrc.current === null || dragOver.current === null || dragSrc.current === dragOver.current) {
            dragSrc.current = null; dragOver.current = null; return;
        }
        const reordered = [...clips];
        const [moved] = reordered.splice(dragSrc.current, 1);
        reordered.splice(dragOver.current, 0, moved);
        dragSrc.current = null; dragOver.current = null;
        onChange(recalcTimestamps(reordered, allowedDurations));
    };

    // ── Mutations ──────────────────────────────────────────────────────────────
    const updateClip = (id: string, patch: Partial<VideoClip>) => {
        const updated = clips.map(c => c.id === id ? { ...c, ...patch } : c);
        onChange(recalcTimestamps(updated, allowedDurations));
    };

    const deleteClip = (id: string) => {
        onChange(recalcTimestamps(clips.filter(c => c.id !== id), allowedDurations));
    };

    const addClip = () => {
        const newClip: VideoClip = {
            id: newClipId(),
            timeline_start: 0,
            duration: allowedDurations[allowedDurations.length - 1] ?? 8,
            storyboard_text: "",
            image_critiques: [],
            image_approved: false,
            video_critiques: [],
            video_approved: false,
        };
        onChange(recalcTimestamps([...clips, newClip], allowedDurations));
    };

    // ── AI fill-in ─────────────────────────────────────────────────────────────
    const handleAiFill = async (clip: VideoClip, index: number) => {
        setFilling(clip.id);
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
            const res = await fetch(`/api/projects/${projectId}/fill-clip`, {
                method: "POST",
                headers,
                body: JSON.stringify({
                    clip_id: clip.id,
                    clip_index: index,
                    total_clips: clips.length,
                    surrounding_context: clips
                        .filter((_, i) => Math.abs(i - index) <= 2 && i !== index)
                        .map(c => c.storyboard_text)
                        .join(" | "),
                    duration: clip.duration,
                }),
            });
            if (!res.ok) throw new Error("Fill failed");
            const data = await res.json();
            updateClip(clip.id, { storyboard_text: data.storyboard_text });
        } catch (e) {
            alert("AI fill-in failed. Try again or write it manually.");
        } finally {
            setFilling(null);
        }
    };

    // ── Render ─────────────────────────────────────────────────────────────────
    return (
        <div className="shot-list">
            {clips.map((clip, index) => (
                <div
                    key={clip.id}
                    className={`shot-card ${dragOver.current === index ? "shot-card--over" : ""}`}
                    draggable
                    onDragStart={() => handleDragStart(index)}
                    onDragEnter={() => handleDragEnter(index)}
                    onDragEnd={handleDragEnd}
                    onDragOver={(e) => e.preventDefault()}
                >
                    {/* Header row */}
                    <div className="shot-header">
                        <div className="shot-drag-handle" title="Drag to reorder">
                            <GripVertical className="w-4 h-4" />
                        </div>
                        <span className="shot-number">Shot {index + 1}</span>

                        {/* Duration input */}
                        <div className="shot-duration-wrap">
                            <select
                                value={quantizeDuration(clip.duration, allowedDurations)}
                                onChange={(e) => updateClip(clip.id, { duration: Number(e.target.value) as 4 | 6 | 8 })}
                                className="shot-duration-input"
                                title={allowedDurations.length === 1 ? "Shot duration (8s only)" : "Shot duration (4s, 6s, or 8s)"}
                            >
                                {allowedDurations.map((duration) => (
                                    <option key={duration} value={duration}>
                                        {duration}
                                    </option>
                                ))}
                            </select>
                            <span className="shot-duration-label">s</span>
                        </div>

                        {/* AI fill-in */}
                        <button
                            className="shot-ai-btn"
                            onClick={() => handleAiFill(clip, index)}
                            disabled={filling === clip.id}
                            title="Ask Gemini to write this shot description"
                        >
                            {filling === clip.id
                                ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                : <Wand2 className="w-3.5 h-3.5" />}
                            <span>{filling === clip.id ? "Generating…" : "AI Fill"}</span>
                        </button>

                        {/* Expand */}
                        {onExpand && (
                            <button
                                className={`shot-expand-btn ${expandedShotId === clip.id ? 'active' : ''}`}
                                onClick={() => onExpand(clip.id)}
                                title="Expand shot editor"
                            >
                                <Maximize2 className="w-3.5 h-3.5" />
                            </button>
                        )}

                        {/* Delete */}
                        <button
                            className="shot-delete-btn"
                            onClick={() => deleteClip(clip.id)}
                            title="Delete this shot"
                            disabled={clips.length <= 1}
                        >
                            <Trash2 className="w-3.5 h-3.5" />
                        </button>
                    </div>

                    {/* Storyboard textarea */}
                    <textarea
                        value={clip.storyboard_text}
                        onChange={(e) => updateClip(clip.id, { storyboard_text: e.target.value })}
                        className="shot-textarea"
                        placeholder="Describe the shot: subject, camera angle, lighting, environment, style…"
                        rows={3}
                    />
                </div>
            ))}

            {/* Add shot button */}
            <button className="shot-add-btn" onClick={addClip}>
                <Plus className="w-4 h-4 mr-1.5" />
                Add Shot
            </button>

            <style jsx>{`
        .shot-list { display: flex; flex-direction: column; gap: 10px; }

        .shot-card {
          background: rgba(255,255,255,0.04);
          border: 1px solid rgba(255,255,255,0.09);
          border-radius: 10px;
          padding: 10px;
          transition: border-color 0.15s, background 0.15s;
          cursor: grab;
        }
        .shot-card:active { cursor: grabbing; }
        .shot-card--over {
          border-color: rgba(99,102,241,0.6);
          background: rgba(99,102,241,0.06);
        }

        .shot-header {
          display: flex;
          align-items: center;
          gap: 8px;
          margin-bottom: 8px;
        }
        .shot-drag-handle {
          color: rgba(255,255,255,0.2);
          cursor: grab;
          flex-shrink: 0;
          padding: 2px;
          border-radius: 4px;
          transition: color 0.15s;
        }
        .shot-drag-handle:hover { color: rgba(255,255,255,0.5); }
        .shot-number {
          font-size: 11px;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.06em;
          color: #818cf8;
          flex: 1;
        }

        .shot-duration-wrap {
          display: flex;
          align-items: center;
          gap: 3px;
          background: rgba(255,255,255,0.06);
          border: 1px solid rgba(255,255,255,0.1);
          border-radius: 6px;
          padding: 3px 6px;
        }
        .shot-duration-input {
          width: 44px;
          background: none;
          border: none;
          outline: none;
          color: rgba(255,255,255,0.8);
          font-size: 12px;
          font-family: monospace;
          text-align: right;
          -moz-appearance: textfield;
        }
        .shot-duration-input option {
          color: #111827;
        }
        .shot-duration-input::-webkit-outer-spin-button,
        .shot-duration-input::-webkit-inner-spin-button { -webkit-appearance: none; }
        .shot-duration-label {
          font-size: 11px;
          color: rgba(255,255,255,0.35);
        }

        .shot-ai-btn {
          display: flex;
          align-items: center;
          gap: 5px;
          background: rgba(99,102,241,0.15);
          border: 1px solid rgba(99,102,241,0.3);
          color: #a5b4fc;
          border-radius: 6px;
          padding: 4px 8px;
          font-size: 11px;
          font-weight: 600;
          cursor: pointer;
          transition: all 0.15s;
          white-space: nowrap;
        }
        .shot-ai-btn:hover { background: rgba(99,102,241,0.25); }
        .shot-ai-btn:disabled { opacity: 0.6; cursor: not-allowed; }

        .shot-expand-btn {
          background: rgba(255,255,255,0.05);
          border: 1px solid rgba(255,255,255,0.1);
          color: rgba(255,255,255,0.5);
          border-radius: 6px;
          padding: 4px 6px;
          cursor: pointer;
          transition: all 0.15s;
          display: flex;
          align-items: center;
          flex-shrink: 0;
        }
        .shot-expand-btn:hover { background: rgba(255,255,255,0.1); color: white; border-color: rgba(255,255,255,0.2); }
        .shot-expand-btn.active { background: rgba(99,102,241,0.2); color: #818cf8; border-color: rgba(99,102,241,0.4); }

        .shot-delete-btn {
          background: none;
          border: 1px solid rgba(239,68,68,0.2);
          color: rgba(239,68,68,0.5);
          border-radius: 6px;
          padding: 4px 6px;
          cursor: pointer;
          transition: all 0.15s;
          display: flex;
          align-items: center;
          flex-shrink: 0;
        }
        .shot-delete-btn:hover { background: rgba(239,68,68,0.1); color: #ef4444; border-color: rgba(239,68,68,0.4); }
        .shot-delete-btn:disabled { opacity: 0.25; cursor: not-allowed; }

        .shot-textarea {
          width: 100%;
          background: rgba(0,0,0,0.25);
          border: 1px solid rgba(255,255,255,0.08);
          border-radius: 7px;
          padding: 8px 10px;
          color: rgba(255,255,255,0.85);
          font-size: 12.5px;
          line-height: 1.55;
          resize: vertical;
          outline: none;
          transition: border-color 0.15s;
          font-family: inherit;
        }
        .shot-textarea:focus { border-color: rgba(99,102,241,0.4); }
        .shot-textarea::placeholder { color: rgba(255,255,255,0.2); }

        .shot-add-btn {
          display: flex;
          align-items: center;
          justify-content: center;
          width: 100%;
          padding: 9px;
          background: rgba(255,255,255,0.03);
          border: 1px dashed rgba(255,255,255,0.15);
          border-radius: 10px;
          color: rgba(255,255,255,0.4);
          font-size: 12px;
          font-weight: 600;
          cursor: pointer;
          transition: all 0.15s;
          margin-top: 2px;
        }
        .shot-add-btn:hover {
          background: rgba(99,102,241,0.08);
          border-color: rgba(99,102,241,0.4);
          color: #a5b4fc;
        }
      `}</style>
        </div>
    );
}
