"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, GripVertical, Loader2, Maximize2, Mic, MicOff, Radio, Send, Volume2, VolumeX } from "lucide-react";
import { DirectorTurn, LiveDirectorResponse, toBackendAssetUrl } from "@/lib/api";
import {
  getLiveDirectorRealtimeLocations,
  getLiveDirectorRealtimeLocation,
  getLiveDirectorRealtimeModel,
  getLiveDirectorRealtimeProjectId,
  getLiveDirectorWsUrl,
  hasLiveDirectorRealtimeConfig,
} from "@/lib/liveDirectorConfig";
import { LiveDirectorAudioCapture, LiveDirectorAudioPlayer } from "@/lib/liveDirectorAudio";
import { LiveDirectorRealtimeEvent, LiveDirectorRealtimeSession } from "@/lib/liveDirectorRealtime";

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
  mode?: "floating" | "docked";
  emptyStateText?: string;
  composerPlaceholder?: string;
  composerHintText?: string;
  onSubmit: (
    message: string,
    source: "text" | "voice",
    speechMode?: "standard" | "realtime",
  ) => Promise<LiveDirectorResponse | null> | LiveDirectorResponse | null;
}

interface WindowPosition {
  x: number;
  y: number;
}

type RealtimeStatus = "unavailable" | "disconnected" | "connecting" | "connected" | "error";

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

function buildRealtimeSystemInstruction(): string {
  return [
    "You are FMV Studio's Live Director voice interface.",
    "For any request that changes, inspects, rewrites, regenerates, adjusts, undoes, or rolls back the user's project, call apply_director_command.",
    "Do not claim that edits were applied until the tool confirms them.",
    "After receiving tool output, explain the result briefly and naturally in spoken form.",
    "You may answer very short greetings or capability questions without a tool call, but project work must go through the tool.",
    "Keep spoken replies concise, collaborative, and director-facing.",
  ].join(" ");
}

function buildRealtimeStatusLabel(status: RealtimeStatus, errorText: string | null): string {
  switch (status) {
    case "connected":
      return "Realtime voice online.";
    case "connecting":
      return "Connecting realtime voice...";
    case "error":
      return errorText || "Realtime voice unavailable. Falling back to standard replies.";
    case "unavailable":
      return "Realtime voice is not configured here. Standard replies remain available.";
    default:
      return "Realtime voice ready to connect.";
  }
}

function shouldRetryRealtimeLocation(message: string): boolean {
  const normalized = message.trim().toLowerCase();
  if (!normalized) return false;
  return (
    normalized.includes("resource exhausted")
    || normalized.includes("resource_exhausted")
    || normalized.includes("quota")
    || normalized.includes("capacity")
    || normalized.includes("temporarily unavailable")
    || normalized.includes("unavailable")
    || normalized.includes("try again")
    || normalized.includes("429")
  );
}

export default function LiveDirectorPanel({
  currentStage,
  focusLabel,
  turns,
  isBusy,
  isProcessing,
  mode = "floating",
  emptyStateText,
  composerPlaceholder,
  composerHintText,
  onSubmit,
}: LiveDirectorPanelProps) {
  const [draft, setDraft] = useState("");
  const [interimTranscript, setInterimTranscript] = useState("");
  const [liveReplyTranscript, setLiveReplyTranscript] = useState("");
  const [isListening, setIsListening] = useState(false);
  const [isOpen, setIsOpen] = useState(true);
  const [isDragging, setIsDragging] = useState(false);
  const [position, setPosition] = useState<WindowPosition | null>(null);
  const [legacySpeechSupported, setLegacySpeechSupported] = useState(false);
  const [autoSpeakReplies, setAutoSpeakReplies] = useState(true);
  const [realtimeStatus, setRealtimeStatus] = useState<RealtimeStatus>(
    hasLiveDirectorRealtimeConfig() ? "disconnected" : "unavailable",
  );
  const [realtimeError, setRealtimeError] = useState<string | null>(null);

  const panelRef = useRef<HTMLDivElement | null>(null);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const dragOffsetRef = useRef<WindowPosition>({ x: 0, y: 0 });
  const listeningSeedRef = useRef("");
  const hydratedReplyIdsRef = useRef<Set<string>>(new Set());
  const replyAudioRef = useRef<HTMLAudioElement | null>(null);
  const realtimeSessionRef = useRef<LiveDirectorRealtimeSession | null>(null);
  const realtimeCaptureRef = useRef<LiveDirectorAudioCapture | null>(null);
  const realtimePlayerRef = useRef<LiveDirectorAudioPlayer | null>(null);
  const latestOnSubmitRef = useRef(onSubmit);
  const currentInputSourceRef = useRef<"text" | "voice">("text");

  const transcriptList = useMemo(() => turns.slice(-10), [turns]);
  const composerValue = mergeDraftAndInterim(draft, hasLiveDirectorRealtimeConfig() ? "" : interimTranscript);
  const canSubmit = !isBusy && !isProcessing && composerValue.length > 0;
  const realtimeEnabled = hasLiveDirectorRealtimeConfig();
  const realtimeStatusLabel = buildRealtimeStatusLabel(realtimeStatus, realtimeError);
  const isDocked = mode === "docked";
  const resolvedEmptyStateText = emptyStateText ?? "No live direction yet. Try “make shot 4 wider and moodier” or “mute the middle audio fragment.”";
  const resolvedComposerPlaceholder = composerPlaceholder ?? "Direct the current stage. Try “make shot 3 tighter.”";

  useEffect(() => {
    latestOnSubmitRef.current = onSubmit;
  }, [onSubmit]);

  const speakWithBrowserVoice = (text: string) => {
    if (typeof window === "undefined" || !("speechSynthesis" in window)) return;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1;
    utterance.pitch = 1;
    utterance.volume = 1;
    window.speechSynthesis.speak(utterance);
  };

  const stopLegacySpeechRecognition = () => {
    recognitionRef.current?.stop();
    setIsListening(false);
  };

  const stopRealtimeCapture = async () => {
    if (!realtimeCaptureRef.current) return;
    if (realtimeSessionRef.current?.isConnected) {
      realtimeCaptureRef.current.emitTrailingSilence();
    }
    await realtimeCaptureRef.current.stop();
    realtimeCaptureRef.current = null;
    setIsListening(false);
  };

  async function handleRealtimeToolCalls(functionCalls: Array<{ id: string; name: string; args?: Record<string, unknown> }>) {
    for (const functionCall of functionCalls) {
      if (functionCall.name !== "apply_director_command") {
        realtimeSessionRef.current?.sendToolResponse(functionCall.id, functionCall.name, {
          ok: false,
          error: `Unsupported function: ${functionCall.name}`,
        });
        continue;
      }

      const message = typeof functionCall.args?.message === "string" ? functionCall.args.message : "";
      if (!message.trim()) {
        realtimeSessionRef.current?.sendToolResponse(functionCall.id, functionCall.name, {
          ok: false,
          error: "Missing message argument.",
        });
        continue;
      }

      const response = await latestOnSubmitRef.current(message, currentInputSourceRef.current, "realtime");
      if (!response) {
        realtimeSessionRef.current?.sendToolResponse(functionCall.id, functionCall.name, {
          ok: false,
          error: "Failed to apply the requested Live Director command.",
        });
        continue;
      }

      realtimeSessionRef.current?.sendToolResponse(functionCall.id, functionCall.name, {
        ok: true,
        reply_text: response.reply_text,
        applied_changes: response.applied_changes,
        target_clip_id: response.target_clip_id || null,
        target_fragment_id: response.target_fragment_id || null,
        stage: response.stage,
      });
    }
  }

  async function handleRealtimeEvent(event: LiveDirectorRealtimeEvent) {
    if (event.type === "setup-complete") {
      setRealtimeStatus("connected");
      setRealtimeError(null);
      return;
    }

    if (event.type === "audio") {
      if (autoSpeakReplies) {
        try {
          await realtimePlayerRef.current?.play(event.data);
        } catch {
          setRealtimeStatus("error");
          setRealtimeError("Live Director audio playback failed.");
        }
      }
      return;
    }

    if (event.type === "input-transcription") {
      setInterimTranscript(event.finished ? "" : event.text);
      return;
    }

    if (event.type === "output-transcription") {
      setLiveReplyTranscript(event.finished ? "" : event.text);
      return;
    }

    if (event.type === "tool-call") {
      await handleRealtimeToolCalls(event.functionCalls);
      return;
    }

    if (event.type === "interrupted") {
      realtimePlayerRef.current?.interrupt();
      setLiveReplyTranscript("");
      return;
    }

    if (event.type === "turn-complete") {
      setInterimTranscript("");
      setLiveReplyTranscript("");
      return;
    }

    if (event.type === "error") {
      realtimeSessionRef.current?.disconnect();
      realtimeSessionRef.current = null;
      void stopRealtimeCapture();
      setRealtimeStatus("error");
      setRealtimeError(event.message);
      return;
    }

    if (event.type === "text") {
      setLiveReplyTranscript(event.text);
    }
  }

  const ensureRealtimeSession = async (): Promise<boolean> => {
    if (!realtimeEnabled) return false;
    if (realtimeSessionRef.current?.isConnected) return true;

    const proxyUrl = getLiveDirectorWsUrl();
    const projectId = getLiveDirectorRealtimeProjectId();
    if (!proxyUrl || !projectId) {
      setRealtimeStatus("unavailable");
      return false;
    }

    try {
      setRealtimeStatus("connecting");
      setRealtimeError(null);

      if (!realtimePlayerRef.current) {
        realtimePlayerRef.current = new LiveDirectorAudioPlayer();
      }

      const model = getLiveDirectorRealtimeModel();
      const preferredLocation = getLiveDirectorRealtimeLocation();
      const candidateLocations = getLiveDirectorRealtimeLocations();
      let lastError: Error | null = null;

      for (let index = 0; index < candidateLocations.length; index += 1) {
        const location = candidateLocations[index];
        const session = new LiveDirectorRealtimeSession({
          proxyUrl,
          model,
          projectId,
          location,
          systemInstruction: buildRealtimeSystemInstruction(),
          voiceName: "Kore",
          onEvent: (event) => {
            void handleRealtimeEvent(event);
          },
        });

        try {
          realtimeSessionRef.current = session;
          await session.connect();
          realtimePlayerRef.current.setMuted(!autoSpeakReplies);
          setRealtimeStatus("connected");
          if (location !== preferredLocation) {
            setRealtimeError(`Realtime voice connected via ${location} after capacity issues in ${preferredLocation}.`);
          }
          return true;
        } catch (error) {
          session.disconnect();
          realtimeSessionRef.current = null;
          lastError = error instanceof Error ? error : new Error("Live Director realtime connection failed.");
          const hasMoreCandidates = index < candidateLocations.length - 1;
          if (!hasMoreCandidates || !shouldRetryRealtimeLocation(lastError.message)) {
            break;
          }
        }
      }

      if (lastError) {
        throw lastError;
      }
      throw new Error("Live Director realtime connection failed.");
    } catch (error) {
      setRealtimeStatus("error");
      setRealtimeError(error instanceof Error ? error.message : "Live Director realtime connection failed.");
      realtimeSessionRef.current = null;
      return false;
    }
  };

  useEffect(() => {
    if (!realtimeEnabled && typeof window !== "undefined") {
      const RecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!RecognitionCtor) {
        setLegacySpeechSupported(false);
        return;
      }

      setLegacySpeechSupported(true);
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
    }
    return undefined;
  }, [realtimeEnabled]);

  useEffect(() => {
    if (isDocked) return undefined;
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
  }, [isDocked, isOpen]);

  useEffect(() => {
    if (isDocked) return undefined;
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
  }, [isDocked, isDragging]);

  useEffect(() => {
    realtimePlayerRef.current?.setMuted(!autoSpeakReplies);
  }, [autoSpeakReplies]);

  useEffect(() => {
    if (!hydratedReplyIdsRef.current.size) {
      hydratedReplyIdsRef.current = new Set(turns.map((turn) => turn.id));
      return;
    }

    if (realtimeStatus === "connected" || realtimeStatus === "connecting") {
      hydratedReplyIdsRef.current = new Set(turns.map((turn) => turn.id));
      return;
    }

    const latestTurn = turns[turns.length - 1];
    if (!latestTurn || latestTurn.role !== "agent") return;
    if (hydratedReplyIdsRef.current.has(latestTurn.id)) return;
    hydratedReplyIdsRef.current.add(latestTurn.id);

    if (!autoSpeakReplies) return;

    const replyAudio = replyAudioRef.current;
    if (replyAudio && latestTurn.audio_url) {
      if (typeof window !== "undefined" && "speechSynthesis" in window) {
        window.speechSynthesis.cancel();
      }
      replyAudio.pause();
      replyAudio.src = toBackendAssetUrl(latestTurn.audio_url);
      replyAudio.currentTime = 0;
      void replyAudio.play().catch(() => {
        speakWithBrowserVoice(latestTurn.text);
      });
      return;
    }

    speakWithBrowserVoice(latestTurn.text);
  }, [autoSpeakReplies, realtimeStatus, turns]);

  useEffect(() => {
    return () => {
      if (typeof window !== "undefined" && "speechSynthesis" in window) {
        window.speechSynthesis.cancel();
      }
      if (replyAudioRef.current) {
        replyAudioRef.current.pause();
        replyAudioRef.current.src = "";
      }
      void stopRealtimeCapture();
      void realtimePlayerRef.current?.destroy();
      realtimeSessionRef.current?.disconnect();
    };
  }, []);

  const handleToggleListening = async () => {
    if (isBusy || isProcessing) return;

    if (realtimeEnabled) {
      if (isListening) {
        await stopRealtimeCapture();
        return;
      }

      const connected = await ensureRealtimeSession();
      if (!connected) return;

      if (!realtimeCaptureRef.current) {
        realtimeCaptureRef.current = new LiveDirectorAudioCapture();
      }

      currentInputSourceRef.current = "voice";
      try {
        await realtimeCaptureRef.current.start((base64Audio, mimeType) => {
          realtimeSessionRef.current?.sendAudioChunk(base64Audio, mimeType);
        });
        setIsListening(true);
      } catch (error) {
        setRealtimeStatus("error");
        setRealtimeError(error instanceof Error ? error.message : "Microphone access failed.");
        realtimeCaptureRef.current = null;
        setIsListening(false);
      }
      return;
    }

    if (!legacySpeechSupported || !recognitionRef.current) return;
    if (isListening) {
      stopLegacySpeechRecognition();
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
    const message = composerValue;
    if (!message) return;

    setDraft("");
    if (!realtimeEnabled) {
      setInterimTranscript("");
    }
    listeningSeedRef.current = "";

    if (realtimeEnabled) {
      const connected = await ensureRealtimeSession();
      if (connected) {
        currentInputSourceRef.current = "text";
        realtimeSessionRef.current?.sendText(message);
        return;
      }
    }

    if (!realtimeEnabled && isListening) {
      stopLegacySpeechRecognition();
    }

    await latestOnSubmitRef.current(message, isListening ? "voice" : "text", "standard");
  };

  const handleComposerKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    void handleSubmit();
  };

  const windowStyle = isDocked
    ? undefined
    : position
      ? { left: `${position.x}px`, top: `${position.y}px`, visibility: "visible" as const }
      : { left: "0px", top: "0px", visibility: "hidden" as const };

  return (
    <div
      ref={panelRef}
      className={isDocked
        ? "h-full w-full overflow-hidden bg-background/40"
        : `fixed z-40 overflow-hidden rounded-[1.6rem] border border-cyan-400/20 bg-[#07131c]/95 shadow-[0_26px_90px_rgba(8,145,178,0.22)] backdrop-blur-xl transition-[width,box-shadow] ${isOpen
            ? "w-[min(24rem,calc(100vw-1.5rem))]"
            : "w-[min(18rem,calc(100vw-1.5rem))]"
          } ${isDragging ? "shadow-[0_30px_110px_rgba(34,211,238,0.28)]" : ""}`}
      style={windowStyle}
    >
      <audio ref={replyAudioRef} hidden preload="none" />
      <div
        onPointerDown={isDocked ? undefined : handleWindowDragStart}
        className={`relative flex items-start justify-between gap-3 border-b border-cyan-400/15 bg-gradient-to-r from-cyan-500/14 via-cyan-400/8 to-transparent px-4 py-3 ${isDocked ? "" : `cursor-grab active:cursor-grabbing ${isDragging ? "cursor-grabbing" : ""}`}`}
      >
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-semibold text-white/95">
            <Radio className="h-4 w-4 text-cyan-300" />
            Live Director
            {!isDocked && <GripVertical className="h-3.5 w-3.5 text-white/35" />}
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
          {!isDocked && (
            <button
              type="button"
              onClick={() => setIsOpen((value) => !value)}
              className="rounded-full border border-white/10 bg-white/5 p-2 text-white/65 transition-colors hover:bg-white/10 hover:text-white"
              title={isOpen ? "Minimize window" : "Expand window"}
              aria-label={isOpen ? "Minimize window" : "Expand window"}
            >
              {isOpen ? <ChevronDown className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
            </button>
          )}
        </div>
      </div>

      {(isDocked || isOpen) && (
        <div className={`flex flex-col ${isDocked ? "h-[calc(100%-4.5rem)]" : "max-h-[min(34rem,calc(100vh-5.5rem))]"}`}>
          <div className="space-y-2 border-b border-white/8 px-4 py-2 text-xs leading-relaxed text-surface-border">
            <div className={`rounded-xl border px-3 py-2 ${realtimeStatus === "connected"
              ? "border-emerald-400/20 bg-emerald-400/10 text-emerald-100/85"
              : realtimeStatus === "error"
                ? "border-amber-400/20 bg-amber-400/10 text-amber-100/85"
                : "border-cyan-400/20 bg-cyan-400/8 text-cyan-100/75"
              }`}>
              {realtimeStatusLabel}
            </div>
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
                {resolvedEmptyStateText}
              </div>
            )}

            {(interimTranscript || liveReplyTranscript) && (
              <div className="space-y-2 rounded-[1.2rem] border border-white/10 bg-white/[0.04] px-3.5 py-3">
                {interimTranscript && (
                  <div className="text-xs text-cyan-100/80">
                    <span className="font-medium text-cyan-200">Listening:</span> {interimTranscript}
                  </div>
                )}
                {liveReplyTranscript && (
                  <div className="text-xs text-white/75">
                    <span className="font-medium text-white/90">Speaking:</span> {liveReplyTranscript}
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="border-t border-white/8 bg-black/20 px-4 py-4">
            <div className="flex items-end gap-3">
              <button
                type="button"
                onClick={() => void handleToggleListening()}
                disabled={(!realtimeEnabled && !legacySpeechSupported) || isBusy || isProcessing}
                className={`flex h-14 w-14 shrink-0 items-center justify-center rounded-full border transition-colors ${((realtimeEnabled || legacySpeechSupported) && !isBusy && !isProcessing)
                  ? isListening
                    ? "border-rose-400/35 bg-rose-400/18 text-rose-100 hover:bg-rose-400/25"
                    : "border-cyan-400/35 bg-cyan-400/18 text-cyan-100 hover:bg-cyan-400/24"
                  : "cursor-not-allowed border-white/10 bg-white/5 text-white/30"
                  }`}
                title={isListening ? "Stop live voice direction" : "Start live voice direction"}
                aria-label={isListening ? "Stop live voice direction" : "Start live voice direction"}
              >
                {isListening ? <MicOff className="h-6 w-6" /> : <Mic className="h-6 w-6" />}
              </button>

              <div className="min-w-0 flex-1 rounded-[1.35rem] border border-white/10 bg-white/[0.05] px-3 py-2">
                <textarea
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={handleComposerKeyDown}
                  className="max-h-28 min-h-[3.25rem] w-full resize-none bg-transparent text-sm leading-relaxed text-white/90 outline-none placeholder:text-white/28"
                  placeholder={resolvedComposerPlaceholder}
                  disabled={isBusy || isProcessing}
                />
                <div className="mt-2 flex items-center justify-between gap-3 text-[11px] text-surface-border">
                  <span>
                    {composerHintText ?? (realtimeEnabled
                      ? "Enter to send through realtime voice. Use the mic for direct speech."
                      : legacySpeechSupported
                        ? "Enter to send. Shift+Enter for a new line."
                        : "Voice capture requires a supported browser or a configured realtime gateway.")}
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
