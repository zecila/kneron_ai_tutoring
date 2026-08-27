/* global AudioWorkletProcessor, sampleRate, registerProcessor */

class TutorSTTPCMCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const requestedRate = options.processorOptions?.outputSampleRate;
    this.outputSampleRate = Number.isFinite(requestedRate) ? requestedRate : 16000;
    this.resampleRatio = sampleRate / this.outputSampleRate;
    this.inputBuffer = new Float32Array(0);
    this.resamplePosition = 0;
    this.outputFrame = new Int16Array(Math.round(this.outputSampleRate / 10));
    this.outputFrameOffset = 0;
  }

  _appendInput(input) {
    const combined = new Float32Array(this.inputBuffer.length + input.length);
    combined.set(this.inputBuffer);
    combined.set(input, this.inputBuffer.length);
    this.inputBuffer = combined;
  }

  _writeSample(sample) {
    const clamped = Math.max(-1, Math.min(1, sample));
    this.outputFrame[this.outputFrameOffset++] = clamped < 0
      ? Math.round(clamped * 0x8000)
      : Math.round(clamped * 0x7fff);

    if (this.outputFrameOffset < this.outputFrame.length) return;
    const buffer = this.outputFrame.buffer;
    this.port.postMessage({ type: "audio", buffer }, [buffer]);
    this.outputFrame = new Int16Array(Math.round(this.outputSampleRate / 10));
    this.outputFrameOffset = 0;
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input?.length) return true;

    this._appendInput(input);
    while (this.resamplePosition + 1 < this.inputBuffer.length) {
      const index = Math.floor(this.resamplePosition);
      const fraction = this.resamplePosition - index;
      const sample = this.inputBuffer[index]
        + (this.inputBuffer[index + 1] - this.inputBuffer[index]) * fraction;
      this._writeSample(sample);
      this.resamplePosition += this.resampleRatio;
    }

    const consumed = Math.min(
      Math.floor(this.resamplePosition),
      this.inputBuffer.length - 1
    );
    if (consumed > 0) {
      this.inputBuffer = this.inputBuffer.slice(consumed);
      this.resamplePosition -= consumed;
    }
    return true;
  }
}

registerProcessor("tutor-stt-pcm-capture", TutorSTTPCMCaptureProcessor);
