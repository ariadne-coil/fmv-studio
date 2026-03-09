export type LiveDirectorRealtimeEvent =
  | { type: "setup-complete" }
  | { type: "turn-complete" }
  | { type: "interrupted" }
  | { type: "audio"; data: string }
  | { type: "input-transcription"; text: string; finished: boolean }
  | { type: "output-transcription"; text: string; finished: boolean }
  | { type: "tool-call"; functionCalls: Array<{ id: string; name: string; args?: Record<string, unknown> }> }
  | { type: "text"; text: string }
  | { type: "error"; message: string };

interface LiveDirectorRealtimeOptions {
  proxyUrl: string;
  model: string;
  projectId: string;
  location: string;
  systemInstruction: string;
  voiceName?: string;
  onEvent: (event: LiveDirectorRealtimeEvent) => void;
}

function parseRealtimeMessage(message: unknown): LiveDirectorRealtimeEvent | null {
  const data = message as any;
  const parts = data?.serverContent?.modelTurn?.parts;
  const inputTranscription = data?.inputTranscription || data?.serverContent?.inputTranscription;
  const outputTranscription = data?.outputTranscription || data?.serverContent?.outputTranscription;

  if (data?.setupComplete) {
    return { type: "setup-complete" };
  }
  if (data?.serverContent?.turnComplete) {
    return { type: "turn-complete" };
  }
  if (data?.serverContent?.interrupted) {
    return { type: "interrupted" };
  }
  if (inputTranscription) {
    return {
      type: "input-transcription",
      text: inputTranscription.text || "",
      finished: !!inputTranscription.finished,
    };
  }
  if (outputTranscription) {
    return {
      type: "output-transcription",
      text: outputTranscription.text || "",
      finished: !!outputTranscription.finished,
    };
  }
  if (data?.toolCall?.functionCalls?.length) {
    return {
      type: "tool-call",
      functionCalls: data.toolCall.functionCalls,
    };
  }
  if (parts?.length && parts[0]?.inlineData?.data) {
    return { type: "audio", data: parts[0].inlineData.data };
  }
  if (parts?.length && parts[0]?.text) {
    return { type: "text", text: parts[0].text };
  }

  return null;
}

export class LiveDirectorRealtimeSession {
  private readonly proxyUrl: string;
  private readonly modelUri: string;
  private readonly systemInstruction: string;
  private readonly voiceName: string;
  private readonly onEvent: (event: LiveDirectorRealtimeEvent) => void;
  private socket: WebSocket | null = null;

  constructor(options: LiveDirectorRealtimeOptions) {
    this.proxyUrl = options.proxyUrl;
    this.systemInstruction = options.systemInstruction;
    this.voiceName = options.voiceName || "Kore";
    this.onEvent = options.onEvent;
    this.modelUri = `projects/${options.projectId}/locations/${options.location}/publishers/google/models/${options.model}`;
  }

  get isConnected(): boolean {
    return this.socket?.readyState === WebSocket.OPEN;
  }

  connect(): Promise<void> {
    if (this.socket && (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)) {
      return Promise.resolve();
    }

    return new Promise((resolve, reject) => {
      const socket = new WebSocket(this.proxyUrl);
      this.socket = socket;

      socket.onopen = () => {
        this.send({
          setup: {
            model: this.modelUri,
            generation_config: {
              response_modalities: ["AUDIO"],
              temperature: 0.5,
              speech_config: {
                voice_config: {
                  prebuilt_voice_config: {
                    voice_name: this.voiceName,
                  },
                },
              },
              enable_affective_dialog: true,
            },
            system_instruction: {
              parts: [{ text: this.systemInstruction }],
            },
            tools: {
              function_declarations: [
                {
                  name: "apply_director_command",
                  description:
                    "Apply any FMV Studio project direction request. Use this for edits, rewrites, shot changes, prompt changes, audio edits, or regeneration requests.",
                  parameters: {
                    type: "OBJECT",
                    properties: {
                      message: {
                        type: "STRING",
                        description: "The user's request in natural language.",
                      },
                    },
                    required: ["message"],
                  },
                },
              ],
            },
            input_audio_transcription: {},
            output_audio_transcription: {},
            realtime_input_config: {
              automatic_activity_detection: {
                disabled: false,
                silence_duration_ms: 1200,
                prefix_padding_ms: 300,
              },
            },
          },
        });
        resolve();
      };

      socket.onerror = () => {
        this.onEvent({ type: "error", message: "Live Director realtime connection failed." });
        reject(new Error("Live Director realtime connection failed."));
      };

      socket.onclose = () => {
        this.socket = null;
      };

      socket.onmessage = (event) => {
        try {
          const parsed = parseRealtimeMessage(JSON.parse(event.data));
          if (parsed) {
            this.onEvent(parsed);
          }
        } catch {
          this.onEvent({ type: "error", message: "Live Director realtime message parsing failed." });
        }
      };
    });
  }

  disconnect(): void {
    this.socket?.close();
    this.socket = null;
  }

  sendText(text: string): void {
    this.send({
      client_content: {
        turns: [
          {
            role: "user",
            parts: [{ text }],
          },
        ],
        turn_complete: true,
      },
    });
  }

  sendAudioChunk(data: string, mimeType: string): void {
    this.send({
      realtime_input: {
        media_chunks: [
          {
            mime_type: mimeType,
            data,
          },
        ],
      },
    });
  }

  sendToolResponse(toolCallId: string, toolName: string, response: Record<string, unknown>): void {
    this.send({
      tool_response: {
        function_responses: [
          {
            id: toolCallId,
            name: toolName,
            response,
          },
        ],
      },
    });
  }

  private send(payload: Record<string, unknown>): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      throw new Error("Live Director realtime session is not connected.");
    }
    this.socket.send(JSON.stringify(payload));
  }
}
