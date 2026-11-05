# -*- coding: utf-8 -*-
"""录音模块：对应 web 端 js/core/audio/recorder.js

- 麦克风采集 16kHz 单声道 int16
- 按 960 样本（60ms）分帧 -> opus 编码 -> 通过 send_fn 发送二进制帧
- stop() 时补齐尾帧并发送空二进制帧作为"录音结束"信号（与 web 端一致）
"""
import numpy as np
import sounddevice as sd

from .opus_codec import OpusEncoder

FRAME_SAMPLES = 960  # 60ms @ 16kHz，与 web 端一致


class AudioRecorder:
    def __init__(self, send_fn, sample_rate=16000, bitrate=16000):
        """
        send_fn: 线程安全发送函数，bytes 参数；send_fn(b"") 表示结束信号
        """
        self.send_fn = send_fn
        self.sample_rate = sample_rate
        self.bitrate = bitrate
        self.encoder = OpusEncoder(sample_rate, bitrate)
        self.recording = False
        self._pcm = np.zeros(0, dtype=np.int16)
        self._stream = None
        self._resampler = None
        self._in_rate = sample_rate

    def start(self):
        if self.recording:
            return
        # flush 后旧编码器已关闭，重新创建
        self.encoder = OpusEncoder(self.sample_rate, self.bitrate)
        self._pcm = np.zeros(0, dtype=np.int16)
        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate, channels=1, dtype="int16",
                callback=self._callback)
            self._in_rate = self.sample_rate
        except sd.PortAudioError:
            # 设备不支持 16kHz 采集时，回退 48kHz + 重采样
            import av
            self._in_rate = 48000
            self._resampler = av.AudioResampler(
                format="s16", layout="mono", rate=self.sample_rate)
            self._stream = sd.InputStream(
                samplerate=48000, channels=1, dtype="int16",
                callback=self._callback)
        self.recording = True
        self._stream.start()

    def stop(self):
        if not self.recording:
            return
        self.recording = False
        # 补齐最后一帧（与 web 端 pad 逻辑一致）
        if self._pcm.size > 0:
            pad = np.zeros(max(0, FRAME_SAMPLES - self._pcm.size), dtype=np.int16)
            for packet in self.encoder.encode(np.concatenate([self._pcm, pad])):
                self.send_fn(packet)
            self._pcm = np.zeros(0, dtype=np.int16)
        for packet in self.encoder.flush():
            self.send_fn(packet)
        # 空二进制帧 = 录音结束信号
        self.send_fn(b"")
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    # ---------- 采集回调 ----------
    def _callback(self, indata, frames, t, status):
        if not self.recording:
            return
        pcm = indata[:, 0].copy()
        if self._resampler is not None:
            pcm = self._resample(pcm)
            if pcm.size == 0:
                return
        self._pcm = np.concatenate([self._pcm, pcm])
        while self._pcm.size >= FRAME_SAMPLES:
            chunk, self._pcm = self._pcm[:FRAME_SAMPLES], self._pcm[FRAME_SAMPLES:]
            for packet in self.encoder.encode(chunk):
                self.send_fn(packet)

    def _resample(self, pcm: np.ndarray) -> np.ndarray:
        import av
        try:
            frame = av.AudioFrame.from_ndarray(
                pcm.reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = self._in_rate
            outs = self._resampler.resample(frame)
            if not outs:
                return np.zeros(0, dtype=np.int16)
            return np.concatenate(
                [f.to_ndarray().reshape(-1) for f in outs]).astype(np.int16)
        except Exception:
            return np.zeros(0, dtype=np.int16)
