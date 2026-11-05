# -*- coding: utf-8 -*-
"""Opus 编解码封装：对应 web 端 js/core/audio/opus-codec.js（libopus.js）。

使用 PyAV（自带 FFmpeg/libopus）实现，无需额外安装 opus DLL。
- 编码：16kHz 单声道 int16 PCM -> opus 包列表（20ms/包，16kbps）
- 解码：单个 opus 包 -> 16kHz 单声道 int16 PCM

注意：FFmpeg/libopus 编码器帧大小为 20ms（320 样本@16kHz），因此一条
960 样本（60ms）的 PCM 会产出 3 个 opus 包；发送时逐包发送即可，
服务器按 opus 包解码，与 web 端 60ms 单包在协议上等价。
"""
import av
import numpy as np


class OpusEncoder:
    def __init__(self, sample_rate=16000, bitrate=16000):
        self.sample_rate = sample_rate
        self.ctx = av.CodecContext.create("libopus", "w")
        self.ctx.sample_rate = sample_rate
        self.ctx.format = "s16"
        self.ctx.layout = "mono"
        self.ctx.bit_rate = bitrate  # 16kbps，对应 web 端 OPUS_SET_BITRATE(16000)

        # 立即打开编码器，读取其要求的帧大小（libopus 通常为 20ms）
        try:
            self.ctx.open()
        except Exception:
            pass
        self.frame_size = int(getattr(self.ctx, "frame_size", 0) or 960)

        self._buf = np.zeros(0, dtype=np.int16)

    def _encode_chunk(self, chunk: np.ndarray):
        frame = av.AudioFrame.from_ndarray(
            chunk.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = self.sample_rate
        return [bytes(p) for p in self.ctx.encode(frame)]

    def encode(self, pcm: np.ndarray) -> list:
        """输入任意长度 int16 单声道 PCM，凑满一帧编码一次，返回 opus 包列表。"""
        self._buf = np.concatenate([self._buf, np.asarray(pcm, dtype=np.int16)])
        out = []
        while len(self._buf) >= self.frame_size:
            chunk = self._buf[: self.frame_size]
            self._buf = self._buf[self.frame_size:]
            try:
                out.extend(self._encode_chunk(chunk))
            except Exception:
                # 编码器要求的帧大小与当前不一致时，按编码器要求重新分帧
                fs = int(getattr(self.ctx, "frame_size", 0) or 0)
                if fs and fs != self.frame_size:
                    self.frame_size = fs
                    self._buf = np.concatenate([chunk, self._buf])
                    continue
                raise
        return out

    def flush(self) -> list:
        """停止时补零对齐并冲刷编码器，返回剩余 opus 包列表。"""
        out = []
        if len(self._buf) > 0:
            pad = np.zeros(self.frame_size - len(self._buf), dtype=np.int16)
            self._buf = np.concatenate([self._buf, pad])
        while len(self._buf) >= self.frame_size:
            chunk = self._buf[: self.frame_size]
            self._buf = self._buf[self.frame_size:]
            try:
                out.extend(self._encode_chunk(chunk))
            except Exception:
                break
        try:
            for p in self.ctx.encode(None):
                out.append(bytes(p))
        except Exception:
            pass
        self._buf = np.zeros(0, dtype=np.int16)
        return out


class OpusDecoder:
    def __init__(self, sample_rate=16000):
        self.sample_rate = sample_rate
        self.ctx = av.CodecContext.create("libopus", "r")
        self.ctx.sample_rate = sample_rate
        self.ctx.format = "s16"
        self.ctx.layout = "mono"
        # libopus 原生解码输出为 48kHz，统一重采样到目标采样率
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)

    def decode(self, packet: bytes) -> np.ndarray:
        """解码单个 opus 包，返回 int16 单声道 PCM。"""
        if not packet:
            return np.zeros(0, dtype=np.int16)
        try:
            frames = self.ctx.decode(av.Packet(bytes(packet)))
        except Exception:
            return np.zeros(0, dtype=np.int16)
        chunks = []
        try:
            for frame in frames:
                for rf in self._resampler.resample(frame):
                    arr = rf.to_ndarray()
                    if arr.ndim == 2:
                        arr = arr[0]
                    if arr.dtype != np.int16:
                        arr = (np.clip(arr, -1.0, 1.0) * 32767).astype(np.int16)
                    chunks.append(arr)
        except Exception:
            pass
        if chunks:
            return np.concatenate(chunks)
        return np.zeros(0, dtype=np.int16)
