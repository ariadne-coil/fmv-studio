"use client";

import React from "react";
import { ExternalLink, FileText, ImageIcon, Music, Plus, Sparkles, Trash2, Video } from "lucide-react";
import { MediaAsset, toBackendAssetUrl } from "@/lib/api";

type UploadAssetType = "audio" | "image" | "video" | "document";

type AssetViewerWorkspaceProps = {
  assets: MediaAsset[];
  selectedAssetId: string | null;
  isBusy?: boolean;
  onSelectAsset: (assetId: string) => void;
  onLabelChange: (assetId: string, nextLabel: string) => void;
  onLabelCommit: (assetId: string) => void;
  onRemoveAsset: (assetId: string) => void;
  onRequestUpload: (type: UploadAssetType) => void;
};

function getAssetTypeIcon(type: string) {
  if (type === "audio") return Music;
  if (type === "video") return Video;
  if (type === "document") return FileText;
  return ImageIcon;
}

function getAssetTypeLabel(type: string): string {
  if (type === "audio") return "Audio";
  if (type === "video") return "Video";
  if (type === "document") return "Document";
  return "Image";
}

function getUploadPrompt(type: UploadAssetType): { title: string; subtitle: string } {
  if (type === "audio") {
    return { title: "Add Audio", subtitle: "Song, stem, or music reference" };
  }
  if (type === "video") {
    return { title: "Add Video", subtitle: "Motion, choreography, or camera reference" };
  }
  if (type === "document") {
    return { title: "Add Document", subtitle: "PDF or DOCX story and world reference" };
  }
  return { title: "Add Image", subtitle: "Character, prop, or location reference" };
}

export default function AssetViewerWorkspace({
  assets,
  selectedAssetId,
  isBusy = false,
  onSelectAsset,
  onLabelChange,
  onLabelCommit,
  onRemoveAsset,
  onRequestUpload,
}: AssetViewerWorkspaceProps) {
  const selectedAsset = assets.find((asset) => asset.id === selectedAssetId) ?? assets[0] ?? null;

  return (
    <div className="flex h-full min-h-0 gap-4 p-4">
      <section className="w-[25rem] min-w-[22rem] glass rounded-xl border border-surface-border/50 overflow-hidden flex min-h-0 flex-col z-10">
        <div className="border-b border-surface-border bg-surface/70 px-4 py-3">
          <h2 className="text-base font-semibold text-white/90">Asset Inspector</h2>
          <p className="mt-1 text-xs text-surface-border">
            Review the selected asset, adjust its AI label, and use Live Director below to rename or prune the library by voice or text.
          </p>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto p-4">
          {selectedAsset ? (
            <div className="space-y-4">
              <div className="rounded-2xl border border-surface-border bg-background/45 overflow-hidden">
                <div className="flex items-center justify-between border-b border-surface-border px-4 py-3 bg-surface/40">
                  <div className="flex items-center gap-2">
                    <span className="rounded-full border border-primary/20 bg-primary/10 px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] text-primary">
                      {getAssetTypeLabel(selectedAsset.type)}
                    </span>
                    {selectedAsset.source === "agent" && (
                      <span className="rounded-full border border-cyan-400/20 bg-cyan-400/10 px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] text-cyan-200">
                        Generated
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <a
                      href={toBackendAssetUrl(selectedAsset.url)}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex h-8 w-8 items-center justify-center rounded-full border border-white/10 bg-white/5 text-white/60 transition-colors hover:bg-white/10 hover:text-white"
                      title="Open asset in new tab"
                    >
                      <ExternalLink className="h-4 w-4" />
                    </a>
                    <button
                      type="button"
                      onClick={() => onRemoveAsset(selectedAsset.id)}
                      className="inline-flex h-8 w-8 items-center justify-center rounded-full border border-rose-500/20 bg-rose-500/10 text-rose-300 transition-colors hover:bg-rose-500/20"
                      title="Delete asset"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </div>

                <div className="aspect-[16/10] bg-black/40">
                  {selectedAsset.type === "image" ? (
                    <img
                      src={toBackendAssetUrl(selectedAsset.url)}
                      alt={selectedAsset.label?.trim() || selectedAsset.name}
                      className="h-full w-full object-contain"
                    />
                  ) : selectedAsset.type === "video" ? (
                    <video
                      src={toBackendAssetUrl(selectedAsset.url)}
                      className="h-full w-full object-contain"
                      controls
                      preload="metadata"
                    />
                  ) : selectedAsset.type === "audio" ? (
                    <div className="flex h-full flex-col items-center justify-center gap-4 px-6 text-center">
                      <Music className="h-12 w-12 text-emerald-300/70" />
                      <p className="text-sm text-white/75">Audio reference loaded for review.</p>
                      <audio
                        src={toBackendAssetUrl(selectedAsset.url)}
                        controls
                        preload="metadata"
                        className="w-full max-w-[20rem]"
                      />
                    </div>
                  ) : (
                    <div className="flex h-full flex-col items-start justify-between p-5 bg-gradient-to-br from-slate-900/95 via-slate-900/80 to-slate-800/50">
                      <FileText className="h-12 w-12 text-white/20" />
                      <p className="text-sm leading-relaxed text-white/70 line-clamp-6">
                        {selectedAsset.ai_context || selectedAsset.text_content || "Document reference ready for the agents."}
                      </p>
                    </div>
                  )}
                </div>
              </div>

              <div className="rounded-2xl border border-surface-border bg-background/45 p-4 space-y-4">
                <div>
                  <label className="mb-1 block text-[11px] uppercase tracking-[0.18em] text-surface-border">
                    AI Label
                  </label>
                  <input
                    value={selectedAsset.label ?? ""}
                    onChange={(event) => onLabelChange(selectedAsset.id, event.target.value)}
                    onBlur={() => onLabelCommit(selectedAsset.id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                        (event.currentTarget as HTMLInputElement).blur();
                      }
                    }}
                    className="w-full rounded-xl border border-surface-border bg-background/70 px-3 py-2.5 text-sm text-white/90 outline-none transition-colors focus:border-primary/40"
                    placeholder="e.g. Mira, Neon Alley, Red Motorcycle"
                  />
                </div>

                <div className="grid grid-cols-2 gap-3 text-xs text-surface-border">
                  <div className="rounded-xl border border-white/8 bg-black/20 px-3 py-2">
                    <div className="text-[10px] uppercase tracking-[0.18em] text-white/40">File</div>
                    <div className="mt-1 truncate text-white/80">{selectedAsset.name}</div>
                  </div>
                  <div className="rounded-xl border border-white/8 bg-black/20 px-3 py-2">
                    <div className="text-[10px] uppercase tracking-[0.18em] text-white/40">Role</div>
                    <div className="mt-1 text-white/80">{getAssetTypeLabel(selectedAsset.type)}</div>
                  </div>
                </div>

                <div className="rounded-xl border border-cyan-400/15 bg-cyan-400/8 px-3 py-3">
                  <div className="mb-1 flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-cyan-200/75">
                    <Sparkles className="h-3.5 w-3.5" />
                    AI-understood context
                  </div>
                  <p className="text-sm leading-relaxed text-white/80 whitespace-pre-wrap">
                    {selectedAsset.ai_context || "No semantic summary was attached to this asset."}
                  </p>
                </div>

                {selectedAsset.text_content && (
                  <div className="rounded-xl border border-white/8 bg-black/20 px-3 py-3">
                    <div className="mb-1 text-[11px] uppercase tracking-[0.18em] text-white/45">
                      Extracted text
                    </div>
                    <p className="max-h-52 overflow-y-auto whitespace-pre-wrap text-sm leading-relaxed text-white/75">
                      {selectedAsset.text_content}
                    </p>
                  </div>
                )}
              </div>
            </div>
          ) : (
            <div className="flex h-full flex-col items-center justify-center rounded-2xl border border-dashed border-surface-border bg-background/40 px-6 text-center">
              <ImageIcon className="h-10 w-10 text-white/25" />
              <h3 className="mt-4 text-sm font-semibold text-white/85">No assets yet</h3>
              <p className="mt-2 max-w-xs text-xs leading-relaxed text-surface-border">
                Upload image, audio, video, or document references to give the orchestrator stronger grounding before you generate storyboards and clips.
              </p>
            </div>
          )}
        </div>
      </section>

      <section className="flex-1 min-h-0 glass rounded-xl border border-surface-border/50 overflow-hidden flex flex-col z-10">
        <div className="flex items-center justify-between border-b border-surface-border bg-surface/70 px-4 py-3">
          <div>
            <h2 className="text-base font-semibold text-white/90">Asset Viewer</h2>
            <p className="mt-1 text-xs text-surface-border">
              Click any asset card to inspect it in detail. Labels here are semantic instructions the agents use throughout planning, storyboarding, and filming.
            </p>
          </div>
          <div className="text-xs text-surface-border">{assets.length} asset{assets.length === 1 ? "" : "s"}</div>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto p-4">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
            {(["audio", "image", "video", "document"] as UploadAssetType[]).map((type) => {
              const Icon = getAssetTypeIcon(type);
              const prompt = getUploadPrompt(type);
              return (
                <button
                  key={type}
                  type="button"
                  onClick={() => onRequestUpload(type)}
                  disabled={isBusy}
                  className={`relative overflow-hidden rounded-xl border border-dashed border-surface-border bg-background/35 p-4 text-left transition-colors ${isBusy ? "cursor-not-allowed opacity-60" : "hover:border-primary/35 hover:bg-primary/5"}`}
                >
                  <div className="flex h-full min-h-[15rem] flex-col items-center justify-center text-center">
                    <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-full border border-white/10 bg-white/5 text-white/65">
                      <Icon className="h-5 w-5" />
                    </div>
                    <div className="text-sm font-semibold text-white/90">{prompt.title}</div>
                    <p className="mt-2 max-w-[14rem] text-xs leading-relaxed text-surface-border">{prompt.subtitle}</p>
                    <div className="mt-4 inline-flex items-center gap-1.5 rounded-full border border-primary/20 bg-primary/10 px-3 py-1 text-[11px] font-medium text-primary">
                      <Plus className="h-3.5 w-3.5" />
                      Upload
                    </div>
                  </div>
                </button>
              );
            })}

            {assets.map((asset) => {
              const Icon = getAssetTypeIcon(asset.type);
              const isSelected = selectedAsset?.id === asset.id;
              return (
                <button
                  key={asset.id}
                  type="button"
                  onClick={() => onSelectAsset(asset.id)}
                  className={`relative overflow-hidden rounded-xl border bg-surface/35 p-4 text-left transition-all ${isSelected
                    ? "border-primary/45 shadow-[0_0_0_1px_rgba(99,102,241,0.35)]"
                    : "border-surface-border hover:border-primary/25 hover:bg-surface-hover/40"
                    }`}
                >
                  <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-surface-border">
                    <Icon className="h-3.5 w-3.5" />
                    {getAssetTypeLabel(asset.type)}
                    {asset.source === "agent" && (
                      <span className="rounded-full border border-cyan-400/20 bg-cyan-400/10 px-2 py-0.5 text-[10px] text-cyan-200">
                        Generated
                      </span>
                    )}
                  </div>

                  <div className="mt-3 overflow-hidden rounded-xl border border-surface-border bg-black/35 aspect-video">
                    {asset.type === "image" ? (
                      <img
                        src={toBackendAssetUrl(asset.url)}
                        alt={asset.label?.trim() || asset.name}
                        className="h-full w-full object-cover"
                      />
                    ) : asset.type === "video" ? (
                      <video
                        src={toBackendAssetUrl(asset.url)}
                        className="h-full w-full object-cover"
                        muted
                        preload="metadata"
                      />
                    ) : asset.type === "audio" ? (
                      <div className="flex h-full flex-col items-center justify-center bg-gradient-to-br from-emerald-500/12 via-background to-background">
                        <Music className="h-10 w-10 text-emerald-300/60" />
                      </div>
                    ) : (
                      <div className="flex h-full flex-col justify-between bg-gradient-to-br from-slate-900/95 via-slate-900/80 to-slate-800/50 p-4">
                        <FileText className="h-8 w-8 text-white/20" />
                        <p className="text-[11px] leading-relaxed text-white/55 line-clamp-4">
                          {asset.ai_context || asset.text_content || "Document reference"}
                        </p>
                      </div>
                    )}
                  </div>

                  <div className="mt-4">
                    <div className="text-sm font-semibold text-white/90">
                      {asset.label?.trim() || asset.name}
                    </div>
                    <div className="mt-1 text-xs text-surface-border truncate">{asset.name}</div>
                  </div>

                  <p className="mt-3 min-h-[3.75rem] text-xs leading-relaxed text-white/60 line-clamp-4">
                    {asset.ai_context
                      || (asset.type === "image"
                        ? "Click to inspect and refine the semantic label used by the agents."
                        : "Click to inspect this asset in more detail.")}
                  </p>
                </button>
              );
            })}
          </div>
        </div>
      </section>
    </div>
  );
}
