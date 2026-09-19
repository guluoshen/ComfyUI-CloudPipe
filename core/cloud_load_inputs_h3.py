# core/cloud/cloud_load_inputs_h3.py
"""
B 节点 —— CloudLoadInputsH3（云端，H3 专用纯管道透传加载器，B 方案）

职责: 从云端 input/ 读 A 上传的 .pt（已含 positive + latent + 可选 negative），
      直接透传给采样链。不做任何文本编码 / ref 注入（那些已在本地
      MiniMaxH3ReferenceToVideo 完成），因此云端不需要 27GB clip 文本编码器。

这是 B 方案「插入式」的核心收益:
  - 本地原生 MiniMaxH3ReferenceToVideo 输出 positive + 空 AV LATENT
  - A 节点原样上传 → 本 B 节点原样读出 → 接 BasicGuider + SamplerCustomAdvanced
  - 云端只跑 diffusion 采样，显存全给模型，且参考图像素从不离开本地

协议: 与 Anima 版一致 —— A 用 torch.save 把 payload 序列化成 .pt 上传；
      B/C 同样用 torch.load/torch.save 读写 .pt，全链路格式一致。

注意: 本节点只输出 positive(conditioning) + LATENT，不输出 PIPE_LINE（H3 采样链是
      BasicGuider+SamplerCustomAdvanced 原生节点组，不是 easy fullkSampler 的 PIPE_LINE 接口）。
"""

import os

import torch

from folder_paths import get_input_directory


class CloudLoadInputsH3:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "管道": ("STRING", {"default": "default", "multiline": False}),
            },
        }

    RETURN_TYPES = ("CONDITIONING", "LATENT")
    RETURN_NAMES = ("positive", "LATENT")
    FUNCTION = "load"
    CATEGORY = "自定义脚本/☁️ 云端/云端用"

    def load(self, 管道):
        input_dir = get_input_directory()
        payload_path = os.path.join(input_dir, f"h3_init_{管道}.pt")

        if not os.path.isfile(payload_path):
            raise Exception(
                f"[B·H3] 缺少初始化文件 (task_id={管道}):\n  {payload_path}\n"
                f"请确认 A 节点已 SFTP 上传。")

        payload = torch.load(payload_path, map_location="cpu", weights_only=False)
        positive = payload.get("positive")
        latent = payload.get("latent")
        negative = payload.get("negative")

        if positive is None or latent is None:
            raise Exception(f"[B·H3] payload 缺少 positive/latent (task_id={管道})")

        # B 方案: 直接透传，不做文本编码 / ref 注入
        print(f"[B·H3] 透传完成 (task_id={管道}): "
              f"positive={type(positive).__name__}, latent={type(latent.get('samples')).__name__}")
        return (positive, latent)


NODE_CLASS = CloudLoadInputsH3
