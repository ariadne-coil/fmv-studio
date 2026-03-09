class LiveDirectorPlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.audioQueue = [];

    this.port.onmessage = (event) => {
      if (event.data === "interrupt") {
        this.audioQueue = [];
        return;
      }
      if (event.data instanceof Float32Array) {
        this.audioQueue.push(event.data);
      }
    };
  }

  process(inputs, outputs) {
    const output = outputs[0];
    if (!output || !output[0]) {
      return true;
    }

    const channel = output[0];
    let outputIndex = 0;

    while (outputIndex < channel.length && this.audioQueue.length > 0) {
      const currentBuffer = this.audioQueue[0];
      const remainingOutput = channel.length - outputIndex;
      const copyLength = Math.min(remainingOutput, currentBuffer.length);

      for (let index = 0; index < copyLength; index += 1) {
        channel[outputIndex + index] = currentBuffer[index];
      }
      outputIndex += copyLength;

      if (copyLength >= currentBuffer.length) {
        this.audioQueue.shift();
      } else {
        this.audioQueue[0] = currentBuffer.slice(copyLength);
      }
    }

    while (outputIndex < channel.length) {
      channel[outputIndex] = 0;
      outputIndex += 1;
    }

    return true;
  }
}

registerProcessor("live-director-playback", LiveDirectorPlaybackProcessor);
