# -*- coding: utf-8 -*-
"""音频播放模块：对应 web 端 js/core/audio/player.js

- 服务器下行 opus 包 -> 解码为 16kHz int16 PCM -> 队列 -> sounddevice 播放
- level 属性输出当前播放音量（0~1），供 Live2D 口型动画使用
"""
import queue
import threading

import av
import numpy as np
import sounddevice as sd

from .opus_codec import OpusDecoder


class AudioPlayer:
    def __init__(self, sample_rate=16000):
        self.sample_rate = sample_rate
        self.decoder = OpusDecoder(sample_rate)
        self.queue = queue.Queue()
        self._buf = np.zeros(0, dtype=np.int16)
        self.level = 0.0
        self._lock = threading.Lock()

        # 优先按 16kHz 打开输出流，失败则回退 48kHz + 重采样
        self.out_rate = sample_rate
        self._resampler = None
        try:
            self.stream = sd.OutputStream(
                samplerate=sample_rate, channels=1, dtype="int16",
                callback=self._callback)
        except sd.PortAudioError:
            self.out_rate = 48000
            self._resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
            self.stream = sd.OutputStream(
                samplerate=48000, channels=1, dtype="int16",
                callback=self._callback)
        self.stream.start()

    # ---------- 数据入口 ----------
    def enqueue(self, opus_bytes: bytes):
        """收到服务器下行 opus 包时调用（空包为结束标志，忽略）。"""
        if not opus_bytes:
            return
        pcm = self.decoder.decode(opus_bytes)
        if pcm.size:
            self.queue.put(pcm)

    def clear(self):
        """清空所有音频缓冲（对应 web 端 clearAllAudio）。"""
        with self.queue.mutex:
            self.queue.queue.clear()
        self._buf = np.zeros(0, dtype=np.int16)
        self.level = 0.0

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass

    # ---------- 播放回调 ----------
    def _callback(self, outdata, frames, t, status):
        need = frames
        idx = 0
        out = outdata[:, 0]
        while need > 0:
            if self._buf.size == 0:
                try:
                    pcm = self.queue.get_nowait()
                except queue.Empty:
                    out[idx:] = 0
                    self.level *= 0.7  # 无数据时音量平滑衰减
                    return
                if self._resampler is not None:
                    pcm = self._resample(pcm)
                self._buf = pcm
            take = min(need, self._buf.size)
            out[idx:idx + take] = self._buf[:take]
            idx += take
            need -= take
            self._buf = self._buf[take:]

        # 计算播放音量，供 Live2D 说话动画使用
        block = out[:frames].astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(block * block)))
        self.level = self.level * 0.6 + min(1.0, rms * 8.0) * 0.4

    def _resample(self, pcm: np.ndarray) -> np.ndarray:
        try:
            frame = av.AudioFrame.from_ndarray(
                pcm.reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = self.sample_rate
            outs = self._resampler.resample(frame)
            if not outs:
                return np.zeros(0, dtype=np.int16)
            return np.concatenate([f.to_ndarray().reshape(-1) for f in outs]).astype(np.int16)
        except Exception:
            return np.zeros(0, dtype=np.int16)
