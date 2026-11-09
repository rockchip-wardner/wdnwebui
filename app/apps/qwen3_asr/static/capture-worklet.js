/**
 * Qwen3-ASR 实时采集 —— AudioWorklet processor。
 *
 * 为什么要单独一个文件：``AudioWorkletProcessor`` 必须注册在
 * **AudioWorkletGlobalScope** 里，而那个作用域只能通过
 * ``audioWorklet.addModule(url)`` 加载 —— 不能像普通脚本那样
 * ``document.head.appendChild``（那是**主线程**的作用域）。
 *
 * 它只做一件事：把 WebAudio 交过来的 Float32 采样**原样**送到主线程。
 *
 * ⚠️ **不在这里重采样**。主线程用**原生采样率**建上下文
 * （``new AudioContext()``，通常 44.1/48 kHz），重采样由主线程用
 * **我们自己的**整块线性插值做 —— 与板载路径（Python 的
 * ``StreamResampler``）**逐样本等价**（清单 B3「跨端一致」的前提）。
 *
 * 为什么不交给浏览器重采样（``new AudioContext({sampleRate: 16000})``）：
 * 那样 WebAudio 会用**它自己的**重采样器（带滤波和延迟），与板载的
 * 线性插值不是同一个算法 → 同一段录音从两端进来可能得到不同的
 * 16 kHz PCM → 文本分叉。
 *
 * 采样率与通道数由主线程在建上下文后读出来 —— 这里不关心。
 */
class AsrCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buf = new Float32Array(8192);
    this._n = 0;
  }

  _emit() {
    if (!this._n) return;
    const out = this._buf.slice(0, this._n);
    this._n = 0;
    // transferable：把 buffer 的所有权交出去，避免复制。
    this.port.postMessage({ samples: out }, [out.buffer]);
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input.length) return true;
    const ch = input[0];
    if (!ch || !ch.length) return true;
    if (this._n + ch.length > this._buf.length) {
      let size = this._buf.length;
      while (size < this._n + ch.length) size *= 2;
      const bigger = new Float32Array(size);
      bigger.set(this._buf.subarray(0, this._n));
      this._buf = bigger;
    }
    this._buf.set(ch, this._n);
    this._n += ch.length;
    // ⚠️ **攒够 4096 个采样再发**（render quantum 只有 128 个 —— 逐
    // quantum 发就是每秒 375 条 postMessage，纯浪费）。重采样是**流式**
    // 的（带 carry），块大小不影响结果。
    if (this._n >= 4096) this._emit();
    return true;
  }
}

registerProcessor("asr-capture", AsrCaptureProcessor);
