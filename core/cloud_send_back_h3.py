# core/cloud/cloud_send_back_h3.py
"""
C 节点 —— CloudSendBackH3（云端，H3 专用纯传回器）

职责: 把 SamplerCustomAdvanced 输出的 denoised AV LATENT（NestedTensor:
      video [B,24,T,H/16,W/16] + audio [B,32,2,T40]）序列化到云端 output/，
      让本地 A 节点拉走做 VAEDecode + VAEDecodeAudio + CreateVideo + SaveVideo。

与 Anima 版 CloudSendBack 的区别:
  - Anima 版: 序列化整个 PIPE_LINE（positive/negative/samples/seed/loader_settings），
              因为下游本地用 easy fullkSampler 的 PIPE_LINE 接口。
  - H3 版:    只序列化 LATENT（AV latent 数值），因为 H3 采样链是原生节点组
              （BasicGuider+SamplerCustomAdvanced），下游本地直接拿 LATENT 做 VAE 解码。

协议: 与 Anima 版一致，统一 .pt (torch.save/torch.load)。
      文件名: cloud_result_<task_id>.h3.pt（与 Anima 的 .pipe.pt 区分，避免误读）。
      注意: 回传的是 LATENT dict {"samples": NestedTensor}，torch.save 可序列化 NestedTensor。
"""

import os

import torch

from folder_paths import get_output_directory


def _save_latent(latent, output_dir, basename):
    """把 LATENT 序列化到云端 output/（.pt 协议，与 A 端 torch.load 对应）

    只存 samples（AV latent 数值）。其它信息（prompt/ref/seed）本地 A 已有，
    不需要回传。
    """
    payload = {
        "samples": latent.get("samples") if isinstance(latent, dict) else latent,
    }
    pt_path = os.path.join(output_dir, basename + ".pt")
    torch.save(payload, pt_path)
    return pt_path


class CloudSendBackH3:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "LATENT": ("LATENT",),
                "task_id": ("STRING", {"default": "default"}),
            },
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("LATENT",)
    FUNCTION = "send_back"
    CATEGORY = "自定义脚本/☁️ 云端/云端用"
    OUTPUT_NODE = True

    def send_back(self, LATENT, task_id):
        if not isinstance(LATENT, dict) or LATENT.get("samples") is None:
            raise Exception("[C·H3] LATENT 输入必须是含 'samples' 的 dict（AV latent）")

        output_dir = get_output_directory()
        os.makedirs(output_dir, exist_ok=True)
        basename = f"cloud_result_{task_id}.h3"
        pt_path = _save_latent(LATENT, output_dir, basename)

        size_mb = os.path.getsize(pt_path) / 1e6
        print(f"[C·H3] 已保存 AV LATENT: {pt_path} ({size_mb:.2f}MB) task_id={task_id}")

        # pass-through 输出（与输入同结构）
        return (LATENT,)


NODE_CLASS = CloudSendBackH3
