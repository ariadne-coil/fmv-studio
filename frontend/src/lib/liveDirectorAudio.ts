const CAPTURE_WORKLET_URL = "/audio-processors/live-director-capture.worklet.js";
const PLAYBACK_WORKLET_URL = "/audio-processors/live-director-playback.worklet.js";
const TARGET_CAPTURE_SAMPLE_RATE = 16000;
const DEFAULT_TRAILING_SILENCE_MS = 1500;
const DEFAULT_TRAILING_SILENCE_CHUNK_MS = 250;

export class LiveDirectorAudioCapture {
  private audioContext: AudioContext | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private mediaStream: MediaStream | null = null;
  private mediaSource: MediaStreamAudioSourceNode | null = null;
  private onChunk: ((base64Audio: string, mimeType: string) => void) | null = null;

  async start(onChunk: (base64Audio: string, mimeType: string) => void): Promise<void> {
    if (this.audioContext) return;

    this.onChunk = onChunk;
    this.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
    });

    this.audioContext = new window.AudioContext({
      sampleRate: TARGET_CAPTURE_SAMPLE_RATE,
    });
    if (this.audioContext.state === "suspended") {
      await this.audioContext.resume();
    }
    await this.audioContext.audioWorklet.addModule(CAPTURE_WORKLET_URL);

    this.workletNode = new AudioWorkletNode(this.audioContext, "live-director-capture");
    this.workletNode.port.onmessage = (event) => {
      if (!(event.data instanceof Float32Array) || !this.onChunk || !this.audioContext) return;
      const pcmBuffer = this.toPCM16(
        event.data,
        this.audioContext.sampleRate,
        TARGET_CAPTURE_SAMPLE_RATE,
      );
      this.onChunk(
        this.arrayBufferToBase64(pcmBuffer),
        `audio/pcm;rate=${TARGET_CAPTURE_SAMPLE_RATE}`,
      );
    };

    this.mediaSource = this.audioContext.createMediaStreamSource(this.mediaStream);
    this.mediaSource.connect(this.workletNode);
  }

  emitTrailingSilence(durationMs = DEFAULT_TRAILING_SILENCE_MS): void {
    if (!this.onChunk) return;

    const chunks = Math.max(1, Math.ceil(durationMs / DEFAULT_TRAILING_SILENCE_CHUNK_MS));
    const samplesPerChunk = Math.max(
      1,
      Math.round((TARGET_CAPTURE_SAMPLE_RATE * DEFAULT_TRAILING_SILENCE_CHUNK_MS) / 1000),
    );
    const silenceBuffer = new Int16Array(samplesPerChunk).buffer;
    const base64Silence = this.arrayBufferToBase64(silenceBuffer);

    for (let index = 0; index < chunks; index += 1) {
      this.onChunk(base64Silence, `audio/pcm;rate=${TARGET_CAPTURE_SAMPLE_RATE}`);
    }
  }

  async stop(): Promise<void> {
    this.workletNode?.disconnect();
    this.workletNode?.port.close();
    this.mediaSource?.disconnect();
    this.mediaStream?.getTracks().forEach((track) => track.stop());
    if (this.audioContext) {
      await this.audioContext.close();
    }

    this.audioContext = null;
    this.workletNode = null;
    this.mediaSource = null;
    this.mediaStream = null;
    this.onChunk = null;
  }

  private toPCM16(
    input: Float32Array,
    inputSampleRate: number,
    outputSampleRate: number,
  ): ArrayBuffer {
    const normalized = inputSampleRate === outputSampleRate
      ? input
      : this.downsample(input, inputSampleRate, outputSampleRate);
    const output = new Int16Array(normalized.length);
    for (let index = 0; index < normalized.length; index += 1) {
      const sample = Math.max(-1, Math.min(1, normalized[index]));
      output[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
    }
    return output.buffer;
  }

  private downsample(input: Float32Array, inputSampleRate: number, outputSampleRate: number): Float32Array {
    if (outputSampleRate >= inputSampleRate) return input;

    const ratio = inputSampleRate / outputSampleRate;
    const outputLength = Math.max(1, Math.round(input.length / ratio));
    const output = new Float32Array(outputLength);
    let outputIndex = 0;
    let inputIndex = 0;

    while (outputIndex < outputLength) {
      const nextInputIndex = Math.min(input.length, Math.round((outputIndex + 1) * ratio));
      let accumulator = 0;
      let count = 0;
      while (inputIndex < nextInputIndex) {
        accumulator += input[inputIndex];
        inputIndex += 1;
        count += 1;
      }
      output[outputIndex] = count > 0 ? accumulator / count : 0;
      outputIndex += 1;
    }

    return output;
  }

  private arrayBufferToBase64(buffer: ArrayBuffer): string {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (let index = 0; index < bytes.byteLength; index += 1) {
      binary += String.fromCharCode(bytes[index]);
    }
    return window.btoa(binary);
  }
}

export class LiveDirectorAudioPlayer {
  private audioContext: AudioContext | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private gainNode: GainNode | null = null;
  private readonly sampleRate = 24000;

  async init(): Promise<void> {
    if (this.audioContext) return;

    this.audioContext = new window.AudioContext({ sampleRate: this.sampleRate });
    await this.audioContext.audioWorklet.addModule(PLAYBACK_WORKLET_URL);
    this.workletNode = new AudioWorkletNode(this.audioContext, "live-director-playback");
    this.gainNode = this.audioContext.createGain();
    this.workletNode.connect(this.gainNode);
    this.gainNode.connect(this.audioContext.destination);
  }

  async play(base64Audio: string): Promise<void> {
    await this.init();
    if (!this.audioContext || !this.workletNode) return;

    if (this.audioContext.state === "suspended") {
      await this.audioContext.resume();
    }

    const binaryString = window.atob(base64Audio);
    const bytes = new Uint8Array(binaryString.length);
    for (let index = 0; index < binaryString.length; index += 1) {
      bytes[index] = binaryString.charCodeAt(index);
    }

    const pcmData = new Int16Array(bytes.buffer);
    const floatData = new Float32Array(pcmData.length);
    for (let index = 0; index < pcmData.length; index += 1) {
      floatData[index] = pcmData[index] / 32768;
    }

    this.workletNode.port.postMessage(floatData);
  }

  interrupt(): void {
    this.workletNode?.port.postMessage("interrupt");
  }

  setMuted(muted: boolean): void {
    if (this.gainNode) {
      this.gainNode.gain.value = muted ? 0 : 1;
    }
  }

  async destroy(): Promise<void> {
    this.workletNode?.disconnect();
    this.gainNode?.disconnect();
    if (this.audioContext) {
      await this.audioContext.close();
    }
    this.audioContext = null;
    this.workletNode = null;
    this.gainNode = null;
  }
}
