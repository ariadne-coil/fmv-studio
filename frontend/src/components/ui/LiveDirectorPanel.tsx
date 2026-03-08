"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, GripVertical, Loader2, Maximize2, Mic, MicOff, Radio, Send, Volume2, VolumeX } from "lucide-react";
import { DirectorTurn } from "@/lib/api";

declare global {
  interface Window {
    SpeechRecognition?: new () => SpeechRecognitionLike;
    webkitSpeechRecognition?: new () => SpeechRecognitionLike;
  }
}

interface SpeechRecognitionAlternativeLike {
  transcript: string;
}

interface SpeechRecognitionResultLike {
  isFinal: boolean;
  0: SpeechRecognitionAlternativeLike;
  length: number;
}

interface SpeechRecognitionEventLike extends Event {
  resultIndex: number;
  results: SpeechRecognitionResultLike[];
}

interface SpeechRecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onerror: ((event: Event) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
}

interface LiveDirectorPanelProps {
  currentStage: string;
  focusLabel?: string | null;
  turns: DirectorTurn[];
  isBusy: boolean;
  isProcessing: boolean;
  onSubmit: (message: string, source: "text" | "voice") => Promise<void> | void;
}

interface WindowPosition {
  x: number;
  y: number;
}

function formatTurnTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function mergeDraftAndInterim(draft: string, interimTranscript: string): string {
  const trimmedDraft = draft.trim();
  const trimmedInterim = interimTranscript.trim();
  return [trimmedDraft, trimmedInterim]
    .filter(Boolean)
    .join(trimmedDraft && trimmedInterim ? " " : "")
    .trim();
}

function clampPanelPosition(position: WindowPosition, width: number, height: number): WindowPosition {
  if (typeof window === "undefined") return position;
  const gutter = 12;
  return {
    x: Math.min(Math.max(gutter, position.x), Math.max(gutter, window.innerWidth - width - gutter)),
    y: Math.min(Math.max(gutter, position.y), Math.max(gutter, window.innerHeight - height - gutter)),
  };
}

export default function LiveDirectorPanel({
  currentStage,
  focusLabel,
  turns,
  isBusy,
  isProcessing,
  onSubmit,
}: LiveDirectorPanelProps) {
  const [draft, setDraft] = useState("");
  const [interimTranscript, setInterimTranscript] = useState("");
  const [isListening, setIsListening] = useState(false);
  const [isOpen, setIsOpen] = useState(true);
  const [isDragging, setIsDragging] = useState(false);
  const [position, setPosition] = useState<WindowPosition | null>(null);
  const [speechSupported, setSpeechSupported] = useState(false);
  const [autoSpeakReplies, setAutoSpeakReplies] = useState(true);

  const panelRef = useRef<HTMLDivElement | null>(null);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const dragOffsetRef = useRef<WindowPosition>({ x: 0, y: 0 });
  const listeningSeedRef = useRef("");
  const hydratedReplyIdsRef = useRef<Set<string>>(new Set());
  const transcriptList = useMemo(() => turns.slice(-10), [turns]);
  const composerValue = mergeDraftAndInterim(draft, interimTranscript);
  const canSubmit = !isBusy && !isProcessing && composerValue.length > 0;

  useEffect(() => {
    if (typeof window === "undefined") return;
    const RecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!RecognitionCtor) {
      setSpeechSupported(false);
      return;
    }

    setSpeechSupported(true);
    const recognition = new RecognitionCtor();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = "en-US";
    recognition.onresult = (event) => {
      let finalTranscript = "";
      let liveTranscript = "";
      for (let index = 0; index < event.results.length; index += 1) {
        const result = event.results[index];
        const piece = result[0]?.transcript ?? "";
        if (result.isFinal) {
          finalTranscript += piece;
        } else {
          liveTranscript += piece;
        }
      }

      const seed = listeningSeedRef.current.trim();
      const mergedFinal = [seed, finalTranscript.trim()].filter(Boolean).join(seed && finalTranscript.trim() ? " " : "");
      setDraft(mergedFinal);
      setInterimTranscript(liveTranscript.trim());
    };
    recognition.onerror = () => {
      setIsListening(false);
    };
    recognition.onend = () => {
      setIsListening(false);
    };
    recognitionRef.current = recognition;

    return () => {
      recognition.stop();
      recognitionRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;

    const syncPosition = () => {
      const rect = panelRef.current?.getBoundingClientRect();
      const width = rect?.width ?? 384;
      const height = rect?.height ?? (isOpen ? 520 : 92);
      setPosition((current) => {
        if (current) return clampPanelPosition(current, width, height);
        return clampPanelPosition(
          {
            x: window.innerWidth - width - 24,
            y: window.innerHeight - height - 24,
          },
          width,
          height,
        );
      });
    };

    const frameId = window.requestAnimationFrame(syncPosition);
    window.addEventListener("resize", syncPosition);
    return () => {
      window.cancelAnimationFrame(frameId);
      window.removeEventListener("resize", syncPosition);
    };
  }, [isOpen]);

  useEffect(() => {
    if (!isDragging || typeof window === "undefined") return;

    const handlePointerMove = (event: PointerEvent) => {
      const rect = panelRef.current?.getBoundingClientRect();
      const width = rect?.width ?? 384;
      const height = rect?.height ?? 520;
      setPosition(
        clampPanelPosition(
          {
            x: event.clientX - dragOffsetRef.current.x,
            y: event.clientY - dragOffsetRef.current.y,
          },
          width,
          height,
        ),
      );
    };

    const handlePointerUp = () => {
      setIsDragging(false);
      document.body.style.userSelect = "";
    };

    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp);

    return () => {
      document.body.style.userSelect = "";
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
    };
  }, [isDragging]);

  useEffect(() => {
    if (!hydratedReplyIdsRef.current.size) {
      hydratedReplyIdsRef.current = new Set(turns.map((turn) => turn.id));
      return;
    }

    const latestTurn = turns[turns.length - 1];
    if (!latestTurn || latestTurn.role !== "agent") return;
    if (hydratedReplyIdsRef.current.has(latestTurn.id)) return;
    hydratedReplyIdsRef.current.add(latestTurn.id);

    if (!autoSpeakReplies || typeof window === "undefined" || !("speechSynthesis" in window)) return;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(latestTurn.text);
    utterance.rate = 1;
    utterance.pitch = 1;
    utterance.volume = 1;
    window.speechSynthesis.speak(utterance);
  }, [autoSpeakReplies, turns]);

  const handleToggleListening = () => {
    if (!speechSupported || !recognitionRef.current || isBusy || isProcessing) return;
    if (isListening) {
      recognitionRef.current.stop();
      setIsListening(false);
      return;
    }

    listeningSeedRef.current = [draft.trim(), interimTranscript.trim()].filter(Boolean).join(draft.trim() && interimTranscript.trim() ? " " : "");
    setInterimTranscript("");
    recognitionRef.current.start();
    setIsListening(true);
  };

  const handleWindowDragStart = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!panelRef.current) return;
    const target = event.target as HTMLElement;
    if (target.closest("button, input, textarea, label, a")) return;

    const rect = panelRef.current.getBoundingClientRect();
    dragOffsetRef.current = {
      x: event.clientX - rect.left,
      y: event.clientY - rect.top,
    };
    setIsDragging(true);
  };

  const handleSubmit = async () => {
    const message = mergeDraftAndInterim(draft, interimTranscript);
    if (!message) return;
    const source: "text" | "voice" = speechSupported && (isListening || listeningSeedRef.current.length > 0) ? "voice" : "text";
    if (isListening && recognitionRef.current) {
      recognitionRef.current.stop();
      setIsListening(false);
    }
    setDraft("");
    setInterimTranscript("");
    listeningSeedRef.current = "";
    await onSubmit(message, source);
  };

  const handleComposerKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    void handleSubmit();
  };

  const windowStyle = position
    ? { left: `${position.x}px`, top: `${position.y}px`, visibility: "visible" as const }
    : { left: "0px", top: "0px", visibility: "hidden" as const };

  return (
    <div
      ref={panelRef}
      className={`fixed z-40 overflow-hidden rounded-[1.6rem] border border-cyan-400/20 bg-[#07131c]/95 shadow-[0_26px_90px_rgba(8,145,178,0.22)] backdrop-blur-xl transition-[width,box-shadow] ${isOpen
        ? "w-[min(24rem,calc(100vw-1.5rem))]"
        : "w-[min(18rem,calc(100vw-1.5rem))]"
        } ${isDragging ? "shadow-[0_30px_110px_rgba(34,211,238,0.28)]" : ""}`}
      style={windowStyle}
    >
      <div
        onPointerDown={handleWindowDragStart}
        className={`relative flex cursor-grab items-start justify-between gap-3 border-b border-cyan-400/15 bg-gradient-to-r from-cyan-500/14 via-cyan-400/8 to-transparent px-4 py-3 active:cursor-grabbing ${isDragging ? "cursor-grabbing" : ""}`}
      >
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-semibold text-white/95">
            <Radio className="h-4 w-4 text-cyan-300" />
            Live Director
            <GripVertical className="h-3.5 w-3.5 text-white/35" />
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-[0.18em] text-cyan-100/65">
            <span className="rounded-full border border-cyan-400/20 bg-cyan-400/10 px-2 py-0.5">
              {currentStage}
            </span>
            {focusLabel && (
              <span className="max-w-[14rem] truncate rounded-full border border-white/10 bg-white/5 px-2 py-0.5 text-white/60 normal-case tracking-normal">
                {focusLabel}
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setAutoSpeakReplies((value) => !value)}
            className={`rounded-full border p-2 transition-colors ${autoSpeakReplies
              ? "border-cyan-400/30 bg-cyan-400/12 text-cyan-100 hover:bg-cyan-400/20"
              : "border-white/10 bg-white/5 text-white/45 hover:text-white/75"
              }`}
            title={autoSpeakReplies ? "Mute spoken replies" : "Enable spoken replies"}
            aria-label={autoSpeakReplies ? "Mute spoken replies" : "Enable spoken replies"}
          >
            {autoSpeakReplies ? <Volume2 className="h-4 w-4" /> : <VolumeX className="h-4 w-4" />}
          </button>
          <button
            type="button"
            onClick={() => setIsOpen((value) => !value)}
            className="rounded-full border border-white/10 bg-white/5 p-2 text-white/65 transition-colors hover:bg-white/10 hover:text-white"
            title={isOpen ? "Minimize window" : "Expand window"}
            aria-label={isOpen ? "Minimize window" : "Expand window"}
          >
            {isOpen ? <ChevronDown className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
          </button>
        </div>
      </div>

      {isOpen && (
        <div className="flex max-h-[min(34rem,calc(100vh-5.5rem))] flex-col">
          <div className="border-b border-white/8 px-4 py-2 text-xs leading-relaxed text-surface-border">
            Give direction in chat form. You can target the current selection or call out any shot by number, like "make shot 4 wider."
          </div>

          <div className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
            {transcriptList.length > 0 ? (
              transcriptList.map((turn) => (
                <div
                  key={turn.id}
                  className={`flex ${turn.role === "user" ? "justify-end" : "justify-start"}`}
                >
                  <div
                    className={`max-w-[88%] rounded-[1.2rem] border px-3.5 py-2.5 ${turn.role === "user"
                      ? "border-cyan-400/20 bg-cyan-400/12 text-white/90"
                      : "border-white/10 bg-white/[0.06] text-white/85"
                      }`}
                  >
                    <div className="flex items-center justify-between gap-3 text-[10px] uppercase tracking-[0.18em]">
                      <span className={turn.role === "user" ? "text-cyan-200/80" : "text-white/45"}>
                        {turn.role === "user" ? "Director" : "Agent"}
                      </span>
                      <span className="text-white/35">{formatTurnTime(turn.created_at)}</span>
                    </div>
                    <p className="mt-1.5 whitespace-pre-wrap text-sm leading-relaxed">
                      {turn.text}
                    </p>
                    {turn.role === "agent" && turn.applied_changes.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {turn.applied_changes.map((item) => (
                          <span
                            key={`${turn.id}-${item}`}
                            className="rounded-full border border-emerald-400/20 bg-emerald-400/10 px-2 py-0.5 text-[10px] text-emerald-200"
                          >
                            {item}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              ))
            ) : (
              <div className="rounded-[1.2rem] border border-dashed border-white/10 bg-white/[0.03] px-4 py-5 text-sm leading-relaxed text-surface-border">
                No live direction yet. Try “make shot 4 wider and moodier” or “mute the middle audio fragment.”
              </div>
            )}
          </div>

          <div className="border-t border-white/8 bg-black/20 px-4 py-4">
            {interimTranscript && (
              <div className="mb-3 rounded-2xl border border-cyan-400/20 bg-cyan-400/8 px-3 py-2 text-xs text-cyan-100/80">
                <span className="font-medium text-cyan-200">Listening:</span> {interimTranscript}
              </div>
            )}

            <div className="flex items-end gap-3">
              <button
                type="button"
                onClick={handleToggleListening}
                disabled={!speechSupported || isBusy || isProcessing}
                className={`flex h-14 w-14 shrink-0 items-center justify-center rounded-full border transition-colors ${speechSupported && !isBusy && !isProcessing
                  ? isListening
                    ? "border-rose-400/35 bg-rose-400/18 text-rose-100 hover:bg-rose-400/25"
                    : "border-cyan-400/35 bg-cyan-400/18 text-cyan-100 hover:bg-cyan-400/24"
                  : "cursor-not-allowed border-white/10 bg-white/5 text-white/30"
                  }`}
                title={isListening ? "Stop voice capture" : "Start voice capture"}
                aria-label={isListening ? "Stop voice capture" : "Start voice capture"}
              >
                {isListening ? <MicOff className="h-6 w-6" /> : <Mic className="h-6 w-6" />}
              </button>

              <div className="min-w-0 flex-1 rounded-[1.35rem] border border-white/10 bg-white/[0.05] px-3 py-2">
                <textarea
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={handleComposerKeyDown}
                  className="max-h-28 min-h-[3.25rem] w-full resize-none bg-transparent text-sm leading-relaxed text-white/90 outline-none placeholder:text-white/28"
                  placeholder="Direct the current stage. Try “make shot 3 tighter.”"
                  disabled={isBusy || isProcessing}
                />
                <div className="mt-2 flex items-center justify-between gap-3 text-[11px] text-surface-border">
                  <span>
                    {speechSupported ? "Enter to send. Shift+Enter for a new line." : "Voice capture requires a supported browser."}
                  </span>
                  <button
                    type="button"
                    onClick={() => void handleSubmit()}
                    disabled={!canSubmit}
                    className={`inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full border transition-colors ${canSubmit
                      ? "border-emerald-400/30 bg-emerald-400/18 text-emerald-100 hover:bg-emerald-400/24"
                      : "cursor-not-allowed border-white/10 bg-white/5 text-white/30"
                      }`}
                    title={isProcessing ? "Applying direction" : "Send direction"}
                    aria-label={isProcessing ? "Applying direction" : "Send direction"}
                  >
                    {isProcessing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
