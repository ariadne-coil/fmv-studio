"use client";

import { useState, useEffect } from "react";
import {
  AppModels,
  AppPreferences,
  DEFAULT_MODELS,
  DEFAULT_PREFERENCES,
  getMusicProviderOption,
  SHOULD_SHOW_API_KEY_SETTINGS,
  getStoredApiKey,
  getStoredModels,
  getStoredPreferences,
  MUSIC_PROVIDER_OPTIONS,
  normalizeMusicProviderId,
  setStoredApiKey,
  setStoredModels,
  setStoredPreferences,
} from "@/lib/api";

interface SettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export default function SettingsModal({ isOpen, onClose }: SettingsModalProps) {
  const [apiKey, setApiKey] = useState("");
  const [models, setModels] = useState<AppModels>(DEFAULT_MODELS);
  const [preferences, setPreferences] = useState<AppPreferences>(DEFAULT_PREFERENCES);
  const [saved, setSaved] = useState(false);
  const [visible, setVisible] = useState(false);
  const selectedMusicProvider = getMusicProviderOption(models.music);

  useEffect(() => {
    if (isOpen) {
      setApiKey(getStoredApiKey());
      setModels(getStoredModels());
      setPreferences(getStoredPreferences());
      setSaved(false);
    }
  }, [isOpen]);

  if (!isOpen) return null;

  const handleSave = () => {
    setStoredApiKey(apiKey);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  const handleClear = () => {
    setApiKey("");
    setStoredApiKey("");
    setSaved(false);
  };

  const maskedKey = apiKey
    ? apiKey.slice(0, 6) + "•".repeat(Math.max(0, apiKey.length - 10)) + apiKey.slice(-4)
    : "";

  return (
    <div
      className="settings-overlay"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="settings-modal">
        {/* Header */}
        <div className="settings-header">
          <div className="settings-title-row">
            <span className="settings-icon">⚙️</span>
            <h2 className="settings-title">Settings</h2>
          </div>
          <button className="settings-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        {SHOULD_SHOW_API_KEY_SETTINGS && (
          <div className="settings-section">
            <label className="settings-label">
              Gemini API Key
              <span className="settings-badge">Stored locally</span>
            </label>
            <p className="settings-description">
              Your key is stored only in your browser&apos;s <code>localStorage</code> and sent directly
              to the backend with each pipeline run. It is never logged or persisted on the server.
            </p>

            <div className="settings-input-row">
              <div className="settings-input-wrap">
                <input
                  type={visible ? "text" : "password"}
                  className="settings-input"
                  placeholder="AIza…"
                  value={apiKey}
                  onChange={(e) => { setApiKey(e.target.value); setSaved(false); }}
                  spellCheck={false}
                  autoComplete="off"
                />
                <button
                  className="settings-eye"
                  onClick={() => setVisible((v) => !v)}
                  title={visible ? "Hide" : "Show"}
                >
                  {visible ? "🙈" : "👁️"}
                </button>
              </div>
              <button className="settings-save-btn" onClick={handleSave}>
                {saved ? "✓ Saved" : "Save"}
              </button>
            </div>

            {apiKey && (
              <div className="settings-status-row">
                <span className="settings-key-preview">{maskedKey}</span>
                <button className="settings-clear" onClick={handleClear}>Clear</button>
              </div>
            )}

            {!apiKey && (
              <p className="settings-fallback-note">
                ℹ️ No key set — the server&apos;s environment key will be used as a fallback.
              </p>
            )}
          </div>
        )}

        {/* Model Selection section */}
        <div className="settings-section mt-6">
          <label className="settings-label">
            Pipeline Models
          </label>
          <p className="settings-description">
            Select which AI models to use for each step of the pipeline.
            Changes to these choices save automatically.
          </p>

          <div className="space-y-4 max-h-[40vh] overflow-y-auto pr-2 custom-scrollbar">
            <div className="flex flex-col gap-1.5">
              <label className="text-sm font-medium text-white/70">Orchestrator (Gemini Thinking)</label>
              <select
                className="w-full bg-surface-hover border border-surface-border rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-primary transition-colors appearance-none"
                value={models.orchestrator}
                aria-label="Orchestrator (Gemini Thinking) Model"
                onChange={(e) => {
                  const nextModels = { ...models, orchestrator: e.target.value };
                  setModels(nextModels);
                  setStoredModels(nextModels);
                  setSaved(false);
                }}
              >
                <option value="gemini-3-pro-preview">Gemini 3 Pro (Default)</option>
                <option value="gemini-3-flash-preview">Gemini 3 Flash</option>
                <option value="gemini-3.1-flash-lite-preview">Gemini 3.1 Flash Lite</option>
              </select>
            </div>

            <div className="flex flex-col gap-1.5">
              <label className="text-sm font-medium text-white/70">Critic (Gemini Review)</label>
              <select
                className="w-full bg-surface-hover border border-surface-border rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-primary transition-colors appearance-none"
                value={models.critic}
                aria-label="Critic (Gemini Review) Model"
                onChange={(e) => {
                  const nextModels = { ...models, critic: e.target.value };
                  setModels(nextModels);
                  setStoredModels(nextModels);
                  setSaved(false);
                }}
              >
                <option value="gemini-3-flash-preview">Gemini 3 Flash (Default)</option>
                <option value="gemini-3.1-flash-lite-preview">Gemini 3.1 Flash Lite</option>
                <option value="gemini-3-pro-preview">Gemini 3 Pro</option>
              </select>
              <p className="text-xs text-surface-border">
                The critic reviews storyboard frames and clips. The default critic path runs without thinking for faster feedback.
              </p>
            </div>

            <div className="flex flex-col gap-1.5">
              <label className="text-sm font-medium text-white/70">Storyboarding (NanoBanana)</label>
              <select
                className="w-full bg-surface-hover border border-surface-border rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-primary transition-colors appearance-none"
                value={models.image}
                aria-label="Storyboarding (NanoBanana) Model"
                onChange={(e) => {
                  const nextModels = { ...models, image: e.target.value };
                  setModels(nextModels);
                  setStoredModels(nextModels);
                  setSaved(false);
                }}
              >
                <option value="gemini-2.5-flash-image">Gemini 2.5 Flash Image (Default)</option>
                <option value="gemini-3-pro-image-preview">NanoBanana Pro</option>
              </select>
            </div>

            <div className="flex flex-col gap-1.5">
              <label className="text-sm font-medium text-white/70">Filming (Veo Video)</label>
              <select
                className="w-full bg-surface-hover border border-surface-border rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-primary transition-colors appearance-none"
                value={models.video}
                aria-label="Filming (Veo Video) Model"
                onChange={(e) => {
                  const nextModels = { ...models, video: e.target.value };
                  setModels(nextModels);
                  setStoredModels(nextModels);
                  setSaved(false);
                }}
              >
                <option value="veo-3.1-fast-generate-001">Veo 3.1 Fast (Default)</option>
                <option value="veo-3.1-generate-001">Veo 3.1 Quality</option>
              </select>
            </div>

            <div className="flex flex-col gap-1.5">
              <label className="text-sm font-medium text-white/70">Music Provider</label>
              <select
                className="w-full bg-surface-hover border border-surface-border rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-primary transition-colors appearance-none"
                value={normalizeMusicProviderId(models.music)}
                aria-label="Music Provider"
                onChange={(e) => {
                  const nextModels = { ...models, music: e.target.value };
                  setModels(nextModels);
                  setStoredModels(nextModels);
                  setSaved(false);
                }}
              >
                {MUSIC_PROVIDER_OPTIONS.map((option) => (
                  <option key={option.id} value={option.id} disabled={!option.available}>
                    {option.label}{option.available ? "" : " (Coming Soon)"}
                  </option>
                ))}
              </select>
              <p className="text-xs text-surface-border">
                {selectedMusicProvider.description}
                {selectedMusicProvider.availabilityNote ? ` ${selectedMusicProvider.availabilityNote}` : ""}
              </p>
              {!selectedMusicProvider.usesLyrics && (
                <p className="text-xs text-amber-300/90">
                  Warning: lyrics will not be used with this instrumental-only provider. Only style and song length affect generation.
                </p>
              )}
            </div>
          </div>
        </div>

        <div className="settings-section mt-6">
          <label className="settings-label">
            Orchestrator Briefs
          </label>
          <p className="settings-description">
            Control whether stage-ready audio summaries are generated and shown alongside the text brief.
          </p>

          <label className="flex items-start gap-3 rounded-xl border border-surface-border bg-background/40 px-4 py-3 text-sm text-white/85">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 rounded border-surface-border bg-background accent-cyan-400"
              checked={preferences.stageVoiceBriefsEnabled}
              onChange={(e) => {
                const nextPreferences = {
                  ...preferences,
                  stageVoiceBriefsEnabled: e.target.checked,
                };
                setPreferences({
                  ...nextPreferences,
                });
                setStoredPreferences(nextPreferences);
                setSaved(false);
              }}
            />
            <span className="space-y-1">
              <span className="block font-medium text-white/90">Enable voice playback for stage briefs</span>
              <span className="block text-xs text-surface-border">
                When disabled, the Orchestrator still writes the text summary, but skips generating the spoken version.
              </span>
            </span>
          </label>
        </div>

        {/* Footer */}
        <div className="settings-footer">
          {SHOULD_SHOW_API_KEY_SETTINGS ? (
            <a
              href="https://aistudio.google.com/apikey"
              target="_blank"
              rel="noopener noreferrer"
              className="settings-link"
            >
              Get a Gemini API key →
            </a>
          ) : (
            <span />
          )}
          <button className="settings-done-btn" onClick={onClose}>Done</button>
        </div>
      </div>

      <style jsx>{`
        .settings-overlay {
          position: fixed;
          inset: 0;
          background: rgba(0, 0, 0, 0.65);
          backdrop-filter: blur(6px);
          display: flex;
          align-items: center;
          justify-content: center;
          z-index: 1000;
          animation: fadeIn 0.15s ease;
        }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

        .settings-modal {
          background: linear-gradient(145deg, #1a1a2e, #16213e);
          border: 1px solid rgba(255,255,255,0.1);
          border-radius: 16px;
          padding: 28px;
          width: 480px;
          max-width: 92vw;
          box-shadow: 0 24px 60px rgba(0,0,0,0.6);
          animation: slideUp 0.2s ease;
        }
        @keyframes slideUp { from { transform: translateY(12px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }

        .settings-header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          margin-bottom: 24px;
        }
        .settings-title-row {
          display: flex;
          align-items: center;
          gap: 10px;
        }
        .settings-icon { font-size: 20px; }
        .settings-title {
          font-size: 18px;
          font-weight: 700;
          color: #fff;
          margin: 0;
        }
        .settings-close {
          background: none;
          border: none;
          color: rgba(255,255,255,0.4);
          font-size: 18px;
          cursor: pointer;
          padding: 4px 8px;
          border-radius: 6px;
          transition: color 0.15s, background 0.15s;
        }
        .settings-close:hover { color: #fff; background: rgba(255,255,255,0.08); }

        .settings-section { margin-bottom: 20px; }

        .settings-label {
          display: flex;
          align-items: center;
          gap: 8px;
          color: rgba(255,255,255,0.85);
          font-size: 14px;
          font-weight: 600;
          margin-bottom: 8px;
        }
        .settings-badge {
          font-size: 10px;
          font-weight: 600;
          background: rgba(99,102,241,0.2);
          color: #a5b4fc;
          border: 1px solid rgba(99,102,241,0.3);
          border-radius: 4px;
          padding: 2px 6px;
          text-transform: uppercase;
          letter-spacing: 0.05em;
        }
        .settings-description {
          font-size: 12px;
          color: rgba(255,255,255,0.45);
          margin-bottom: 14px;
          line-height: 1.6;
        }
        .settings-description code {
          background: rgba(255,255,255,0.07);
          padding: 1px 5px;
          border-radius: 3px;
          font-family: monospace;
        }

        .settings-input-row {
          display: flex;
          gap: 10px;
          align-items: stretch;
        }
        .settings-input-wrap {
          flex: 1;
          position: relative;
          display: flex;
          align-items: center;
        }
        .settings-input {
          width: 100%;
          background: rgba(255,255,255,0.06);
          border: 1px solid rgba(255,255,255,0.12);
          border-radius: 8px;
          padding: 10px 38px 10px 12px;
          color: #fff;
          font-size: 13px;
          font-family: monospace;
          outline: none;
          transition: border-color 0.15s;
        }
        .settings-input:focus { border-color: rgba(99,102,241,0.6); }
        .settings-input::placeholder { color: rgba(255,255,255,0.25); }
        .settings-eye {
          position: absolute;
          right: 10px;
          background: none;
          border: none;
          cursor: pointer;
          font-size: 14px;
          padding: 0;
          line-height: 1;
        }
        .settings-save-btn {
          background: linear-gradient(135deg, #6366f1, #8b5cf6);
          color: #fff;
          border: none;
          border-radius: 8px;
          padding: 10px 20px;
          font-size: 13px;
          font-weight: 600;
          cursor: pointer;
          white-space: nowrap;
          transition: opacity 0.15s, transform 0.1s;
        }
        .settings-save-btn:hover { opacity: 0.9; transform: translateY(-1px); }
        .settings-save-btn:active { transform: translateY(0); }

        .settings-status-row {
          display: flex;
          align-items: center;
          justify-content: space-between;
          margin-top: 10px;
        }
        .settings-key-preview {
          font-family: monospace;
          font-size: 12px;
          color: rgba(255,255,255,0.4);
        }
        .settings-clear {
          background: none;
          border: 1px solid rgba(239,68,68,0.3);
          color: rgba(239,68,68,0.7);
          border-radius: 6px;
          padding: 3px 10px;
          font-size: 11px;
          cursor: pointer;
          transition: all 0.15s;
        }
        .settings-clear:hover { background: rgba(239,68,68,0.1); color: #ef4444; }

        .settings-fallback-note {
          margin-top: 10px;
          font-size: 12px;
          color: rgba(255,200,80,0.7);
        }

        .settings-footer {
          display: flex;
          align-items: center;
          justify-content: space-between;
          margin-top: 24px;
          padding-top: 20px;
          border-top: 1px solid rgba(255,255,255,0.07);
        }
        .settings-link {
          font-size: 12px;
          color: rgba(99,102,241,0.8);
          text-decoration: none;
          transition: color 0.15s;
        }
        .settings-link:hover { color: #a5b4fc; }
        .settings-done-btn {
          background: rgba(255,255,255,0.07);
          border: 1px solid rgba(255,255,255,0.12);
          color: rgba(255,255,255,0.8);
          border-radius: 8px;
          padding: 8px 20px;
          font-size: 13px;
          font-weight: 500;
          cursor: pointer;
          transition: all 0.15s;
        }
        .settings-done-btn:hover { background: rgba(255,255,255,0.12); color: #fff; }
      `}</style>
    </div>
  );
}
