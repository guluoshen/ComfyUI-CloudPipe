# core/cloud_pipe_sender.py
"""
A 节点 —— CloudPipeAsync（本地，闭环发+收）

职责:
  输入 pipe (PIPE_LINE)
  → SFTP 上传 cond/latent 到云端 input/
  → 拼 cloud_wf (UNETLoader + CLIPLoader + B with task_id + easy fullkSampler + C)
  → 提交云端 /prompt API (走 SSH 隧道 127.0.0.1:6006)
  → 轮询 /view 试探 cloud_result_<task_id>.pipe.pt 出现
  → SFTP 下载 + 反序列化为 PIPE_LINE
  → 用本地 model/clip/vae 重组 pipe 输出

关键设计:
  - cond/latent 走 torch.save(.pt) 整个对象忠实序列化 → SFTP 上传到云端 input/
    (不手工拆 tensor/extras, 云端 torch.load 还原出与本地采样器接收时完全一致的 cond)
  - B 节点的"管道"是 STRING 触发器 (task_id) → B 从 input/ 读 cond 文件组装 PIPE_LINE
  - 采样由 cloud_wf 里的 easy fullkSampler 独立完成 (ABC 不参与采样)
  - 输出 pipe 结构 100% 等于输入 pipe (本地 VAEDecode 直接解包)
"""

import os
import time
import json
import uuid
import asyncio
import copy

import torch
import requests
import comfy.samplers as comfy_samplers

from folder_paths import get_output_directory
from .cloud_tunnel import TUNNEL, ensure_tunnel


# 云端连接/路径说明(2026-08-22 改版: 全部走自建隧道 cloud_tunnel, 不再硬编码 SSH 凭据)
#   - SSH 凭据改由全局 CLOUD_CONN(云端运维节点写入) 提供, 经自建隧道复用同一条 SSH 会话;
#   - 本地 http_url(隧道转发端口)由 cloud_tunnel.ensure 动态分配, 不在此硬编码;
#   - 云端 input/output 目录由全局安装目录推导(见 _cloud_input_dir / _cloud_output_dir)。
CLOUD_URL = "http://127.0.0.1:6006"  # 仅作回落默认值(实际用隧道本地转发端口)

# 云端资源路径
CLOUD_UNET = "anima/Anima-2.9B-preview-v1.safetensors"
CLOUD_CLIP = "anima/qwen_3_06b_base.safetensors"
# LoRA: 由云端已有加载器固定加载 (Easy-Use easy loraStack + Comfyroll CR Apply LoRA Stack,
# model+clip 都合并)。A 端不再提供 Lora 参数。云端 loras 目录需存在此文件。
CLOUD_LORA = "xyz/baisi_anima_lora.safetensors"


def _squeeze_to_4d(t):
    """把 5 维图像 latent (B,4,1,H,W) 规整成 4 维 (B,4,H,W)。

    仅用于标准 SD 图像 VAE 的 latent 被意外多包了一层时间轴（C=4）。
    若 5 维但 C=16/48 等视频/多通道 VAE 格式 → 返回 None，调用方应保留 5 维。
    """
    if t.dim() != 5:
        return None
    if t.shape[1] != 4:
        # 非 4 通道 5 维，大概率是 Wan/Qwen/Cosmos 等视频 VAE 的合法 5 维 latent，不能 squeeze
        return None
    s = t.squeeze()
    if s.dim() == 4:
        return s
    if s.dim() == 3:
        return s.unsqueeze(0)
    return None


def _unwrap_samples(samples):
    """重组兜底: 把 C 回传的 samples 规整成 VAEDecode 可接收的 LATENT {"samples": tensor}

    云端 easy fullkSampler 输出的 new_pipe["samples"] 直接交给本机 VAEDecode，
    形态必须与本地 VAE 匹配：
      - 标准图像 VAE (4 通道) → 4 维 (B,4,H,W)
      - Qwen/Wan 等 image_vae (16 通道 3D 卷积) → 5 维 (B,16,F,H,W)，F 常为 1
    若把 Qwen 的 5 维 latent 误 squeeze 成 4 维，本机 VAEDecode 会在 sd.py 取 shape[4] 越界。
    """
    if samples is None:
        print("[A][WARN] _unwrap_samples: samples 为 None（云端未回传）")
        return None

    def _make(tensor):
        """根据通道数判断 5 维是否应保留。"""
        if tensor.dim() == 4:
            return {"samples": tensor}
        if tensor.dim() == 5:
            c = tensor.shape[1]
            if c in (16, 48):
                # Wan/Qwen/Cosmos 等多通道 3D 卷积 VAE 的合法 5 维 latent，保留原样
                print(f"[A][INFO] _unwrap_samples: 保留 5 维多通道 latent shape={tuple(tensor.shape)} (C={c})")
                return {"samples": tensor}
            if c == 4:
                s = _squeeze_to_4d(tensor)
                if s is not None:
                    print(f"[A][INFO] _unwrap_samples: 4 通道 5 维已 squeeze 为 4维 shape={tuple(s.shape)}")
                    return {"samples": s}
            print(f"[A][WARN] _unwrap_samples: 5 维 tensor 无法处理 shape={tuple(tensor.shape)}")
            return None
        print(f"[A][WARN] _unwrap_samples: tensor 维度异常 dim={tensor.dim()} shape={tuple(tensor.shape)}")
        return None

    if isinstance(samples, torch.Tensor):
        return _make(samples)
    if isinstance(samples, dict):
        inner = samples.get("samples")
        if isinstance(inner, torch.Tensor):
            return _make(inner)
        if isinstance(inner, dict):
            return _unwrap_samples(inner)
        # 含非 tensor 的 "samples"，尝试找任意张量值
        for v in samples.values():
            if isinstance(v, torch.Tensor):
                return _make(v)
        keys = list(samples.keys())
        print(f"[A][WARN] _unwrap_samples: dict 内无合法 tensor（keys={keys}）")
        return None
    print(f"[A][WARN] _unwrap_samples: 无法识别的 samples 类型 {type(samples)}")
    return None


def _cloud_input_dir():
    """云端 input/ 目录(正斜杠), 由全局安装目录推导"""
    from .cloud_shared import get_inst_dir
    return get_inst_dir().rstrip("/") + "/input/"


def _cloud_output_dir():
    """云端 output/ 目录(正斜杠), 由全局安装目录推导"""
    from .cloud_shared import get_inst_dir
    return get_inst_dir().rstrip("/") + "/output/"


# 本地中转目录: 云端上传/下载的 .pt 临时文件统一放这里, 不污染 ComfyUI/input。
# 默认 <你的 ComfyUI 根目录>/temp, 不存在则自动创建。
# __file__ = .../custom_nodes/自定义脚本/core/cloud/cloud_pipe_sender.py
#   向上 5 层 (dirname 链): cloud -> core -> 自定义脚本 -> custom_nodes -> ComfyUI
_HERE = os.path.abspath(__file__)
_COMFY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))))
_CLOUD_LOCAL_TMP = os.path.join(_COMFY_ROOT, "temp")


def cloud_temp_dir():
    """返回本地云端中转目录并自动创建（ComfyUI 根下的 temp 目录）。"""
    os.makedirs(_CLOUD_LOCAL_TMP, exist_ok=True)
    return _CLOUD_LOCAL_TMP

TIMEOUT = 3600
POLL_INTERVAL = 5


def _save_cond(cond, path):
    """忠实序列化 cond (list of [tensor, extras]) → 单文件 torch.save

    关键: 不手工拆解 tensor / extras, 直接 torch.save 整个 cond 对象。
    torch.save(pickle) 会保留每个张量的 dtype/shape/device, 以及 extras 里哪些是
    tensor、哪些是 list/scalar 的嵌套结构 —— 云端 torch.load 还原出来的 cond 与本地
    采样器接收时**逐字节一致**, 即"本地采样器接收什么, 云端就接收什么"。

    这彻底消除之前手工 safetensors + 手写 json 重建的整类 bug:
      - 只把 pooled_output 还原成 tensor、其它 tensor 字段漏还原成 list
        → 'list' object has no attribute 'unsqueeze'
      - 整段 extras 被丢 → Anima (qwen3) conditioning 不完整 → 纯噪点
      - 强制 .float() 改写 cond dtype → 与模型不匹配
    """
    torch.save(cond, path)
    return os.path.getsize(path)


def _save_latent(samples, path):
    """忠实序列化 samples (LATENT dict {"samples": tensor}) → torch.save

    samples 直接是 ComfyUI LATENT 结构, 云端 torch.load 还原出完全相同的 dict,
    不再经 LoadLatent 二次转换。
    """
    torch.save(samples, path)
    return os.path.getsize(path)


def _save_init(samples, seed, loader_settings, path):
    """打包初始画布: samples(LATENT) + seed + loader_settings → 单 .pt

    与 B 端 cloud_load_inputs._read_cond / torch.load 对应:
    B 从 cloud_init_<task_id>.pt 读回 samples/seed/loader_settings 组装 PIPE_LINE。
    （V3.1 修复：此前只存 samples，seed/loader_settings 丢失）
    """
    payload = {
        "samples": samples,
        "seed": int(seed or 0),
        "loader_settings": loader_settings or {},
    }
    torch.save(payload, path)
    return os.path.getsize(path)


def _sftp_upload(local_path, remote_path):
    """SFTP 上传: 复用自建隧道的同一条 SSH 会话(取代每次新建 paramiko 连接)"""
    TUNNEL.sftp_put(local_path, remote_path)


def _sftp_download(remote_path, local_path):
    """SFTP 下载: 复用自建隧道的同一条 SSH 会话"""
    TUNNEL.sftp_get(remote_path, local_path)


def _load_pipe_from_local(pt_path):
    """从云端 C 节点 torch.save 的 .pt 反序列化为 PIPE_LINE (不含 model/clip/vae)

    C 节点把 positive/negative/samples/seed/loader_settings 整包 torch.save,
    A 端 torch.load 原样还原 —— 不挑 key、不补默认、不重建 cond 结构。
    """
    cloud_pipe = torch.load(pt_path, map_location="cpu", weights_only=False)
    return cloud_pipe


def _notify(title, message, kind="info"):
    """推前端 Toast 通知 (ComfyUI 0.33 PromptServer.send_json_event)"""
    try:
        from server import PromptServer
        PromptServer.instance.send_json_event("notification", {
            "title": title, "message": message, "kind": kind,
        })
    except Exception:
        pass


def _format_bytes(n):
    """把字节数转人类可读 (B/KB/MB/GB)，用于面板显示上传/下载大小"""
    try:
        n = int(n)
    except Exception:
        return "?"
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    return f"{n / 1024 / 1024 / 1024:.2f}GB"


def _send_cloud_progress(task_id, text):
    """把云端协作进度推到前端 CloudComfyOp 节点的「状态」widget。

    task_id 用于前端区分不同任务（H3 / 图片 可能并发）。
    事件名 cloud_op_progress，前端 js/cloud_comfy_op.js 监听并写入「状态」。
    """
    try:
        from server import PromptServer
        PromptServer.instance.send_json_event("cloud_op_progress", {
            "task_id": task_id, "text": text,
        })
    except Exception:
        pass


def run_cloud_test(http_url=CLOUD_URL):
    """连通测试 (模块级纯函数): 造 dummy 管道 → SFTP 上传 → 云端 CloudTestEcho 原样回传
    → 下载对比 → 返回 PASS/FAIL 消息字符串（含当前连接模式：隧道/SSH）。

    由前端「连接并检测」/ 测试接口调用:
    - 不走 ComfyUI 图执行 → 点击即测、几秒出结果, 不等上游节点跑完;
    - 不返回管道 → 不会把 4D latent 传给下游 VAEDecode (根治 tuple index out of range);
    - 云端 CloudTestEcho 不加载任何模型 (无卡也能执行), 只验证「本地↔云端」数据传递无损。
    - 通道: 统一走自建隧道(本地转发端口), 文件传输经同一条 SSH 会话的 SFTP 子系统。
    """
    from .cloud_transport import detect_mode, post_prompt, get_history, probe_output, fetch_output
    # 统一走自建隧道: 确保隧道已建立(读全局连接建/复用), 拿到本地转发 url
    url = ensure_tunnel()
    mode, mode_cn, url = detect_mode(url)
    http_url = url
    task_id = uuid.uuid4().hex[:12]
    local_input = cloud_temp_dir()

    # 造 dummy 管道 (与采样无关, 只测传输)
    dummy = {
        "positive": [[torch.randn(1, 77, 1024) * 0.5, {"pooled_output": None}]],
        "negative": [[torch.randn(1, 77, 1024) * 0.5, {"pooled_output": None}]],
        "samples": {"samples": torch.randn(1, 4, 64, 64) * 0.5},
        "seed": 12345,
        "loader_settings": {"steps": 30, "cfg": 4.0, "sampler_name": "euler", "scheduler": "simple"},
    }
    local_pt = os.path.join(local_input, f"test_in_{task_id}.pt")
    torch.save(dummy, local_pt)
    _sftp_upload(local_pt, f"{_cloud_input_dir()}test_in_{task_id}.pt")
    print(f"[A] 测试模式: 已上传 dummy 数据 (task_id={task_id}, 连接模式={mode_cn})")

    # 云端只跑 echo 节点, 不加载模型
    cloud_wf = {"1": {"class_type": "CloudTestEcho", "inputs": {"管道": task_id}}}
    client_id = f"cloudTest_{task_id}"
    pid = post_prompt(http_url, {"prompt": cloud_wf, "client_id": client_id}, mode, client_id)
    print(f"[A] 测试模式: 已提交 echo (pid={pid}, {mode_cn})")

    # 轮询回传文件（等云端 status==success 且文件就绪，防 torch.save 半写）
    cloud_basename = f"cloud_result_{task_id}.pipe"
    filename = cloud_basename + ".pt"
    deadline = time.time() + 180
    last_err = None
    while time.time() < deadline:
        time.sleep(2)
        try:
            hist = get_history(http_url, pid, mode)
            entry = hist.get(pid) or {}
            st = entry.get("status", {}).get("status_str")
            if st == "error":
                msgs = entry["status"].get("messages", [])
                raise Exception(f"[A] 测试失败(云端执行错误): {json.dumps(msgs[-1], ensure_ascii=False)[:300] if msgs else '未知'}")
            ready, _content = probe_output(http_url, filename, mode)
            if st == "success" and ready:
                break
        except Exception as e:
            if "测试失败" in str(e):
                raise
            last_err = e
            continue
    else:
        raise Exception(f"[A] 测试模式超时 (180s), 最后异常: {last_err}")

    # 下载回传（按模式；失败再回退 SFTP 直取）
    local_out = os.path.join(cloud_temp_dir(), filename)
    try:
        fetch_output(http_url, filename, local_out, mode)
    except Exception as e:
        print(f"[A] fetch_output({mode_cn}) 失败 ({e}), 回退 SFTP 直取")
        _sftp_download(f"{_cloud_output_dir()}{filename}", local_out)
    back = torch.load(local_out, map_location="cpu", weights_only=False)

    # 逐项对比
    def _t(v):
        return v["samples"] if isinstance(v, dict) and "samples" in v else v
    def _cmp(a, b, name):
        a, b = _t(a), _t(b)
        if tuple(a.shape) != tuple(b.shape):
            return f"{name} 形状不一致 {tuple(a.shape)} vs {tuple(b.shape)}"
        md = (a - b).abs().max().item()
        return f"{name} 形状={tuple(a.shape)} 最大差值={md:.6f}"
    r1 = _cmp(dummy["samples"], back.get("samples"), "samples")
    r2 = _cmp(dummy["positive"][0][0], back["positive"][0][0], "pos")
    r3 = _cmp(dummy["negative"][0][0], back["negative"][0][0], "neg")
    ok = all("不一致" not in x and "0.000000" in x for x in (r1, r2, r3))
    result_msg = f"[{mode_cn}通道] PASS ✅ 本地↔云端数据无损 ({r1}; {r2}; {r3})" if ok \
        else f"[{mode_cn}通道] FAIL ❌ 数据有损 ({r1}; {r2}; {r3})"
    print(f"[A] 测试模式: {r1} | {r2} | {r3}")
    print(f"[A] 测试模式: {result_msg}")
    # 推前端 Toast 通知 (ComfyUI 0.33)
    _notify("云端连通测试", result_msg, kind="success" if ok else "error")
    return result_msg


class CloudPipeAsync:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "管道": ("PIPE_LINE",),
            },
            "optional": {
                # ---- 采样参数 (本地 widget, 云端直接用, 无需打开云端 ComfyUI) ----
                # 默认对齐 Anima 官方 blueprint (image_anima_base_v1.json):
                # steps=30, cfg=4.0, euler, simple, denoise=1.0
                # 注意: 只声明 easy fullkSampler INPUT_TYPES 里真实存在的参数
                # (required: pipe/steps/cfg/sampler_name/scheduler/denoise/image_output/
                # link_id/save_prefix; optional: seed/model/positive/negative/latent/
                # vae/clip/xyPlot/image)。force_full_denoise(全降噪)/add_noise(加噪)/
                # disable_noise/control_after_generate(生成后控制) 不在其列,
                # 灌进 inputs 会被 ComfyUI 静默忽略 → 不提供对应 widget。
                "步骤": ("INT", {"default": 30, "min": 1, "max": 10000}),
                "CFG": ("FLOAT", {"default": 4.0, "min": 0.0, "max": 100.0, "step": 0.1}),
                "采样器": (comfy_samplers.KSampler.SAMPLERS, {"default": "euler"}),
                "调度器": (comfy_samplers.KSampler.SCHEDULERS, {"default": "simple"}),
                "降噪": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
                "种子": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF,
                              "control_after_generate": True}),
                "图像输出": (["None", "Hide", "Preview", "Save"], {"default": "None"}),
            },
        }

    # 系统默认：单条 pipe 原样发云端，原样返回，不拆批、不并发、不区分模式。
    RETURN_TYPES = ("PIPE_LINE", "STRING")
    RETURN_NAMES = ("管道", "结果")
    OUTPUT_IS_LIST = (False, False)
    OUTPUT_NODE = True
    FUNCTION = "send_and_wait"
    CATEGORY = "自定义脚本/☁️ 云端/本机用"

    def _listen_progress(self, http_url, client_id):
        """后台线程: WebSocket 订阅云端 ComfyUI 的 progress/executing 事件, print 采样进度到控制台

        云端 ComfyUI 原生向 clientId 推送进度 ({"type":"progress","data":{"value":7,"max":30}}),
        A 端无需任何云端改动, 只需保持 client_id 与提交 /prompt 时一致。
        """
        import threading
        import json as _json

        def _run():
            try:
                import websocket
            except ImportError:
                print("[A] 进度监听需要 websocket-client (pip install websocket-client), 已跳过 (不影响功能)")
                return
            ws_url = http_url.replace("http://", "ws://") + f"/ws?clientId={client_id}"
            try:
                ws = websocket.create_connection(ws_url, timeout=5)
            except Exception:
                return
            try:
                while True:
                    msg = ws.recv()
                    if not msg:
                        break
                    try:
                        data = _json.loads(msg)
                    except Exception:
                        continue
                    mtype = data.get("type")
                    if mtype == "progress":
                        d = data.get("data") or {}
                        v = d.get("value", 0)
                        m = d.get("max", 1)
                        if m:
                            print(f"[A] 云端采样进度: {v}/{m} ({v / m * 100:.0f}%)", flush=True)
                    elif mtype == "executing":
                        d = data.get("data") or {}
                        if d.get("node") is None:
                            break
            except Exception:
                pass
            finally:
                try:
                    ws.close()
                except Exception:
                    pass

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t

    def send_and_wait(self, 管道,
                       步骤=30, CFG=4.0, 采样器="euler", 调度器="simple", 降噪=1.0,
                       种子=0, 图像输出="None"):
        """A 节点入口（系统默认，无模式、不拆分、无并发）

        输入 pipe 原样发云端采样，云端返回的结果 pipe 原样交下游 VAEDecode。
        不区分正常/流水线，不在本节点做 batch 拆分合并。
        """
        from .cloud_pipe_batch_sender import CloudPipeBatchAsync

        # 单值/列表兼容（某些情况下 ComfyUI 会把 INT/FLOAT 包成 list）
        if isinstance(种子, (list, tuple)):
            种子 = 种子[0]

        # 系统默认：管道原样发云端，不拆批。
        if not isinstance(管道, dict):
            raise Exception("[A] 输入 pipe 非 dict（系统默认仅支持单条 pipe）")

        results, summary = CloudPipeBatchAsync().send_and_wait_batch(
            管道,
            步骤, CFG, 采样器, 调度器, 降噪, 种子, 图像输出)

        return (results, summary)



# ---------------------------------------------------------------------------
# 连通测试接口: 前端 A 节点「测试」按钮调用 (不走图执行, 点击即测)
#  - POST /custom_script/cloud_pipe_test  body={"云端地址": "http://127.0.0.1:6006"}
#  - 同步执行 run_cloud_test (~4s: SFTP 上传 + 云端 CloudTestEcho echo + 下载对比)
#  - 返回 {"ok": true/false, "message": "PASS/FAIL ..."}
# 注意: 路由注册必须 try/except 保护 —— PromptServer.instance 在 server 初始化前
#       不存在, 模块级裸注册会 AttributeError → nodes.py 裸 import 失败 →
#       整个自定义脚本包加载失败 → 所有云端节点菜单消失 (V3.1 已修)。
# ---------------------------------------------------------------------------
try:
    from server import PromptServer
    from aiohttp import web


    @PromptServer.instance.routes.post("/custom_script/cloud_pipe_test")
    async def cloud_pipe_test_route(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        # 统一走自建隧道: run_cloud_test 内部 ensure_tunnel() 建/复用隧道, 此处不再依赖 云端地址
        try:
            loop = asyncio.get_running_loop()
            msg = await loop.run_in_executor(None, run_cloud_test)
            return web.json_response({"ok": True, "message": msg})
        except Exception as e:  # noqa: BLE001 - 测试失败也返回 json, 由前端展示
            return web.json_response({"ok": False, "message": str(e)})
except Exception as _route_e:
    print(f"[自定义脚本] 警告: cloud_pipe_test 路由注册失败(不影响节点加载): {_route_e}")


NODE_CLASS = CloudPipeAsync
