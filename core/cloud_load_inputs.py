# core/cloud_load_inputs.py
"""
B 节点 —— CloudLoadInputs（云端，纯管道加载器）

职责: 从云端 input/ 读 A 上传的 cond/latent 文件 + 用云端 model/clip 组装 PIPE_LINE 输出
  - 入: model (MODEL) + clip (CLIP) + 管道 (STRING, task_id 触发器)
  - 出: 管道 (PIPE_LINE)                                    ← 单管道输出
  - 内部: 从 input/ 读 pos_cond_<task_id>.pt / neg_cond_<task_id>.pt
                 / cloud_init_<task_id>.pt (含 latent + seed + loader_settings)
         组装 PIPE_LINE (含 model/clip/cond/latent/seed/loader_settings)
  - 不调 KSampler, 不加载 vae, 不接文件路径参数
  - 采样由云端画布上的 easy fullkSampler 接管道完成

协议统一说明（V3.1 修复）: A 节点用 torch.save 把 cond/latent 序列化成 .pt 上传;
B/C 节点同样用 torch.load / torch.save 读写 .pt, 全链路格式一致。
此前 B 仍读 safetensors+.json+.latent, 与 A 上传的 .pt 文件名/格式对不上,
导致 A→B→C 图片工作流闭环无法执行 (『ABC 传递文件不一致』bug), 已统一修复。
"""

import os

import torch

from folder_paths import get_input_directory


def _normalize_samples(samples):
    """把任意形态的 latent/samples 规整成 easy fullkSampler 期望的标准 LATENT: {"samples": tensor}

    注意 Qwen/Wan 等多通道 image VAE 的 latent 是 5 维 (B,C=16,F,H,W)，必须保留 5 维：
    若误把 16 通道 5 维 squeeze 成 4 维，本机 VAEDecode 取 shape[4] 会越界。
    仅当 5 维且 C=4（标准 SD latent 被多包一层时间轴）才 squeeze 成 4 维。
    """
    if samples is None:
        raise Exception("[B] samples 为空: A 端上传的 cloud_init 里没有 samples（latent）")
    if isinstance(samples, dict) and "samples" in samples:
        inner = samples["samples"]
        if isinstance(inner, torch.Tensor):
            return {"samples": _fix_latent_dim(inner)}
        if isinstance(inner, dict):
            return _normalize_samples(inner)
        return _normalize_samples(inner)
    if isinstance(samples, torch.Tensor):
        return {"samples": _fix_latent_dim(samples)}
    if isinstance(samples, dict):
        for k, v in samples.items():
            if isinstance(v, torch.Tensor):
                return {"samples": _fix_latent_dim(v)}
        raise Exception(f"[B] samples 是 dict 但找不到 tensor 值, 键={list(samples.keys())}")
    raise Exception(f"[B] samples 形态异常: {type(samples)} (期望 tensor 或含 'samples' 键的 dict)")


def _fix_latent_dim(t):
    """规整 latent tensor 维度数:
      - 4 维 → 原样
      - 5 维且 C(=dim1) in {16,48} → 保留 5 维 (Qwen/Wan/Cosmos 多通道 VAE)
      - 5 维且 C==4 → squeeze 多余单维成 4 维
      - 其它不可恢复 → 抛异常
    """
    if t.dim() == 4:
        return t
    if t.dim() == 5:
        if t.shape[1] in (16, 48):
            print(f"[B][INFO] _normalize_samples: 保留 5 维多通道 latent shape={tuple(t.shape)} (C={t.shape[1]})")
            return t
        if t.shape[1] == 4:
            s = t.squeeze()
            if s.dim() == 4:
                print(f"[B][INFO] _normalize_samples: 4 通道 5 维已 squeeze 为 4维 shape={tuple(s.shape)}")
                return s
            if s.dim() == 3:
                return s.unsqueeze(0)
        raise Exception(f"[B] 5 维 latent 无法归一化 shape={tuple(t.shape)}")
    raise Exception(f"[B] latent 维度异常 dim={t.dim()} shape={tuple(t.shape)}")


def _normalize_cond(cond, name):
    """校验 cond 是标准 CONDITIONING: list of [tensor, extras_dict]

    easy fullkSampler 内部 convert_cond 假设每个元素 c = [tensor, dict]。
    这里兜底: 若 cond 是单条 [tensor, dict]（非 list 包裹），包成 list；
    若元素是 (tensor, dict) tuple，转成 [tensor, dict]；其余形态抛清晰异常。
    """
    if cond is None:
        raise Exception(f"[B] {name} 为空: A 端未上传 {name} 的 .pt")
    # 单条 cond（A 端未包成 list of list）：[tensor, dict] → 包成 [单条]
    if (isinstance(cond, (list, tuple)) and len(cond) == 2
            and isinstance(cond[0], torch.Tensor) and not isinstance(cond[0], (list, tuple))):
        return [list(cond)]
    if not isinstance(cond, (list, tuple)):
        raise Exception(f"[B] {name} 形态异常: {type(cond)} (期望 list of [tensor, dict])")
    out = []
    for i, c in enumerate(cond):
        if not (isinstance(c, (list, tuple)) and len(c) >= 2):
            raise Exception(f"[B] {name}[{i}] 不是 [tensor, dict] 结构: {type(c)}")
        t, extras = c[0], c[1]
        if not isinstance(t, torch.Tensor):
            raise Exception(f"[B] {name}[{i}][0] 不是 tensor: {type(t)}")
        if not isinstance(extras, dict):
            # extras 不是 dict（可能是 None），补空 dict，避免 easy-use 后续 .copy() 崩
            extras = {} if extras is None else dict(extras) if isinstance(extras, dict) else {}
        out.append([t, extras])
    return out


def _read_cond(pt_path):
    """读 A 上传的 cond .pt (torch.save 的 list of [tensor, extras]) → list of [tensor, extras]"""
    if not os.path.isfile(pt_path):
        return None
    cond = torch.load(pt_path, map_location="cpu", weights_only=False)
    return cond


class CloudLoadInputs:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "clip": ("CLIP",),
                "管道": ("STRING", {"default": "default", "multiline": False}),
            },
        }

    RETURN_TYPES = ("PIPE_LINE",)
    RETURN_NAMES = ("管道",)
    FUNCTION = "load"
    CATEGORY = "自定义脚本/☁️ 云端/云端用"

    def load(self, model, clip, 管道):
        input_dir = get_input_directory()

        pos_path = os.path.join(input_dir, f"pos_cond_{管道}.pt")
        neg_path = os.path.join(input_dir, f"neg_cond_{管道}.pt")
        init_path = os.path.join(input_dir, f"cloud_init_{管道}.pt")

        # 检查 cond 文件是否存在（A 必须先 SFTP 上传）
        if not os.path.isfile(pos_path) or not os.path.isfile(neg_path):
            raise Exception(
                f"[B] 缺少条件文件 (task_id={管道}):\n  {pos_path}\n  {neg_path}\n"
                f"请确认 A 节点已 SFTP 上传到云端 input/。")
        if not os.path.isfile(init_path):
            raise Exception(
                f"[B] 缺少初始画布 (task_id={管道}):\n  {init_path}\n"
                f"请确认 A 节点已 SFTP 上传。")

        # 读 cond（list of [tensor, extras]，由 A 用 torch.save 忠实序列化）
        pos = _read_cond(pos_path)
        neg = _read_cond(neg_path)

        # 读 init（含 samples(LATENT dict) + seed + loader_settings）
        init = torch.load(init_path, map_location="cpu", weights_only=False)
        raw_samples = init.get("samples")
        seed = int(init.get("seed", 0) or 0)
        loader_settings = init.get("loader_settings") or {}

        # ---- 归一化: 让 pipe 数据 100% 兼容 easy fullkSampler（不改 KSampler 一行） ----
        # 兜底 samples 任意嵌套层数 → 标准 {"samples": tensor}
        samples = _normalize_samples(raw_samples)
        # 兜底 positive/negative 为标准 CONDITIONING: list of [tensor, dict]
        pos = _normalize_cond(pos, "positive")
        neg = _normalize_cond(neg, "negative")

        # 组装 PIPE_LINE
        new_pipe = {
            "model": model,
            "clip": clip,
            "vae": None,           # vae 留给 A 重组时填本地值
            "positive": pos,
            "negative": neg,
            "samples": samples,
            "images": None,
            "seed": seed,
            "loader_settings": loader_settings,
        }

        print(f"[B] 加载完成 (task_id={管道}): pos={len(pos)} 层, neg={len(neg)} 层, "
              f"seed={seed}, ls={list(loader_settings.keys())[:3]}...")
        return (new_pipe,)


NODE_CLASS = CloudLoadInputs
