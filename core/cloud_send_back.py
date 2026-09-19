# core/cloud_send_back.py
"""
C 节点 —— CloudSendBack（云端，纯传回器）

职责: 把 KSampler 采样后的 PIPE_LINE 序列化到云端 output/ 让 A 拉走
  - 入: 管道 (PIPE_LINE) + task_id (STRING)    ← 只这 2 个
  - 出: 管道 (PIPE_LINE)                        ← 输入输出同结构 (pass-through)
  - 内部: 从管道取 samples/positive/negative/seed/loader_settings 序列化
    model/clip/vae 不存 (本地有, 不需要传)
  - KSampler 由画布上的 Easy-Use fullkSampler 完成, 输出新管道已含新 samples

协议统一说明（V3.1 修复）: A/B/C 全链路统一为 .pt (torch.save/torch.load)。
此前 C 写 safetensors+.json, A 端等待的是 cloud_result_<task_id>.pipe.pt,
文件名/格式对不上导致闭环失败, 已统一修复。
"""

import os

import torch

from folder_paths import get_output_directory


def _save_pipe(pipe, output_dir, basename):
    """把 PIPE_LINE 序列化到云端 output/（.pt 协议，与 A 端 torch.load 对应）

    只存 A 重组时需要、云端本地没有的字段: positive / negative / samples / seed /
    loader_settings。model/clip/vae 不存（本地有，不需要传）。
    """
    payload = {
        "positive": pipe.get("positive") or [],
        "negative": pipe.get("negative") or [],
        "samples": pipe.get("samples") or {},
        "seed": int(pipe.get("seed", 0) or 0),
        "loader_settings": pipe.get("loader_settings") or {},
    }

    pt_path = os.path.join(output_dir, basename + ".pt")
    torch.save(payload, pt_path)
    return pt_path


class CloudSendBack:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "管道": ("PIPE_LINE",),
                "task_id": ("STRING", {"default": "default"}),
            },
        }

    RETURN_TYPES = ("PIPE_LINE",)
    RETURN_NAMES = ("管道",)
    FUNCTION = "send_back"
    CATEGORY = "自定义脚本/☁️ 云端/云端用"
    OUTPUT_NODE = True

    def send_back(self, 管道, task_id):
        if not isinstance(管道, dict):
            raise Exception("[C] 管道输入必须是 PIPE_LINE 类型的 dict")

        # 序列化整个 PIPE_LINE (不含 model/clip/vae) 到云端 output/
        output_dir = get_output_directory()
        os.makedirs(output_dir, exist_ok=True)
        basename = f"cloud_result_{task_id}.pipe"
        pt_path = _save_pipe(管道, output_dir, basename)

        size_mb = os.path.getsize(pt_path) / 1e6
        print(f"[C] 已保存 PIPE_LINE: {pt_path} ({size_mb:.2f}MB) task_id={task_id}")

        # pass-through 输出 (输入输出同结构)
        return (管道,)


NODE_CLASS = CloudSendBack
