# core/cloud/cloud_pipe_sender_h3.py
"""
A 节点 —— CloudPipeAsyncH3（本地，H3 视频云端协作闭环发+收，B 方案/插入式）

架构（与云端图片工作流同源「云端只跑最吃显存的一步，其余本地，媒体不跨云端」）:
  - 本地原生 MiniMaxH3ReferenceToVideo 节点已经完成: ref 媒体 VAE 编码 + 文本编码，
    输出 positive(CONDITIONING) + LATENT(空 AV latent)。
  - 本 A 节点直接插在 MiniMaxH3ReferenceToVideo 后面，接收 positive/latent，
    打包成 .pt 上传云端（媒体不出本地、只传数值 cond/latent）。
  - 云端 B 节点只做透传（不需要 27GB clip），采样链跑完后 C 节点回传 denoised AV latent。
  - 本地 A 收到 denoised LATENT 后交给下游 VAEDecode+VAEDecodeAudio+CreateVideo+SaveVideo。

为什么是「插入式」(B 方案):
  H3 原生 MiniMaxH3ReferenceToVideo 把 ref 编码 + 文本编码耦合在一个节点，本地能跑
  （不跑采样）。本 A 节点不再重复编码，只负责「把原生节点产物上传 → 云端采样 → 回传」，
  因此能无缝串在 MiniMaxH3ReferenceToVideo 之后:
      LoadImage/LoadAudio → MiniMaxH3ReferenceToVideo → [A·H3] → VAEDecode+... → CreateVideo → SaveVideo
  云端不需要文本编码模型（clip），显存全给 diffusion 采样，且参考图像素不上云。

本文件不修改任何 Anima CloudPipe 节点，是独立的新三件套之一（A/B/C 同目录）。
"""

import os
import time
import json
import uuid

import torch
import comfy.nested_tensor

from .cloud_tunnel import TUNNEL, ensure_tunnel
from .cloud_transport import detect_mode, post_prompt, get_history, probe_output
from .cloud_shared import get_inst_dir
from .cloud_pipe_sender import (
    cloud_temp_dir,
    _sftp_upload, _sftp_download,
    _cloud_input_dir, _cloud_output_dir,
    _notify,
    _send_cloud_progress, _format_bytes,
    TIMEOUT, POLL_INTERVAL,
)


# 云端 H3 diffusion 资源路径。
# ⚠️ 关键：UNETLoader 的 unet_name 取的是 folder_paths.get_filename_list("diffusion_models")，
# 该列表递归扫描 models/{unet,diffusion_models}/ 后**只返回相对子目录前缀**
# （如 "minimax/文件名"），不含 "diffusion_models/" 这一层键名。
# 云端实测返回值（2026-08-23 经 SSH 核实）：
#   ITEM: minimax/minimax_h3_ref2va_pruned_int8_convrot.safetensors
# 故此处只能写 "minimax/文件名"，多写 "diffusion_models/" 会触发 value_not_in_list。
# 对应云端的真实落盘路径：models/diffusion_models/minimax/（diffusion_models 是键，非前缀）。
CLOUD_H3_UNET = "minimax/minimax_h3_ref2va_pruned_int8_convrot.safetensors"


# ---------------------------------------------------------------------------
# 上传 / 提交 / 轮询 / 下载
# ---------------------------------------------------------------------------
def _upload_h3(local_payload_pt, task_id):
    remote = f"{_cloud_input_dir()}h3_init_{task_id}.pt"
    _sftp_upload(local_payload_pt, remote)
    size = _format_bytes(os.path.getsize(local_payload_pt))
    _send_cloud_progress(task_id, f"[H3] 已上传 cond/latent ({size}) → 云端采样中…")
    return remote


def _h3_progress_listener(http_url, client_id, task_id):
    """后台线程: WebSocket 订阅云端 ComfyUI 的 progress 事件 → 推前端「状态」面板。

    复用云端原生 clientId 进度推送 ({"type":"progress","data":{"value":n,"max":m}})。
    异常只打印, 不影响主流程轮询。
    """
    import threading
    import json as _json

    def _run():
        try:
            import websocket
        except ImportError:
            print("[A·H3] 进度监听需要 websocket-client, 已跳过(不影响采样)")
            return
        ws_url = http_url.replace("http://", "ws://") + f"/ws?clientId={client_id}"
        try:
            ws = websocket.create_connection(ws_url, timeout=5)
        except Exception as e:
            print(f"[A·H3] 进度 WS 连接失败({e}), 已跳过")
            return
        step_times = []
        last_t = time.time()
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
                    m = d.get("max", 1) or 1
                    now = time.time()
                    if step_times:
                        step_times.append(now - last_t)
                    last_t = now
                    pct = v / m * 100
                    eta = ""
                    if len(step_times) >= 2:
                        avg = sum(step_times[-6:]) / len(step_times[-6:])
                        remain = max(0, int(avg * (m - v)))
                        eta = f" · 约剩 {remain//60}分{remain%60}秒" if remain >= 60 else f" · 约剩 {remain}秒"
                    _send_cloud_progress(
                        task_id,
                        f"[H3] 云端采样 {v}/{m} ({pct:.0f}%){eta}")
                elif mtype == "executing":
                    d = data.get("data") or {}
                    if d.get("node") is None:
                        break
        except Exception as e:
            print(f"[A·H3] 进度 WS 监听结束({e})")
        finally:
            try:
                ws.close()
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()


def _resolve_cloud_log_path():
    """解析云端 ComfyUI 运行日志路径：优先 autostart 重定向的 /root/comfyui_auto.log，
    退而 {install_dir}/comfyui.log。返回首个存在的路径（均不存在则兜底第一个）。"""
    candidates = ["/root/comfyui_auto.log"]
    inst = get_inst_dir() or "/root/autodl-tmp/ComfyUI"
    candidates.append(f"{inst}/comfyui.log")
    for c in candidates:
        try:
            out, _ = TUNNEL.ssh_exec(f"test -f '{c}' && echo FOUND || echo MISSING")
            if out and "FOUND" in out:
                return c
        except Exception:
            continue
    return candidates[0]


def _follow_cloud_log(log_path, last_line):
    """增量 tail 云端日志新增行，返回 (new_text, new_last_line)。
    行数回退(被截断/轮转)时从头输出，避免漏看。"""
    try:
        tot_out, _ = TUNNEL.ssh_exec(f"wc -l < '{log_path}' 2>/dev/null || echo 0")
        total = int(((tot_out or "0").strip().split() or ["0"])[0])
    except Exception:
        return "", last_line
    if total < last_line:
        last_line = 0
    if total <= last_line:
        return "", last_line
    try:
        new_out, _ = TUNNEL.ssh_exec(f"tail -n +{last_line + 1} '{log_path}'")
    except Exception as e:
        return f"[跟随失败: {e}]", last_line
    return (new_out or ""), total


def _cloud_alive(http_url):
    """轻量探测云端 ComfyUI 是否还活着。经隧道本地端口 GET /system_stats；
    任何连接/超时异常或非常规状态 → 返回 False（云端实例已终止/不可达）。
    云端正常服务时返回 200 → True。与业务轮询解耦，避免把『云端死』当普通异常吞掉。"""
    try:
        import requests
        r = requests.get(f"{http_url}/system_stats", timeout=5,
                         proxies={"http": None, "https": None})
        return r.status_code == 200
    except Exception:
        return False


def _submit_and_wait_h3(task_id, cloud_wf, http_url, mode, mode_cn, params):
    client_id = f"cloudH3_{task_id}"
    pid = post_prompt(http_url, {"prompt": cloud_wf, "client_id": client_id}, mode, client_id)
    print(f"[A·H3] 任务已提交 (pid={pid}, task_id={task_id}, 模式={mode_cn})")

    # 启动 WS 进度监听(后台线程, 异常不影响主流程)
    _h3_progress_listener(http_url, client_id, task_id)

    cloud_basename = f"cloud_result_{task_id}.h3"
    view_url = f"{http_url}/view?filename={cloud_basename}.pt&type=output"
    deadline = time.time() + TIMEOUT
    last_err = None
    # 云端日志实时跟随（每 ~10s 增量 tail 打印到本地 ComfyUI 终端；主循环串行，不并发 SSH）
    _log_path = None
    _last_line = 0
    _last_log_t = time.time()
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        # 云端存活检测：SSH/HTTP 一断立即结束，不傻等 TIMEOUT（3600s）整 1 小时
        if not _cloud_alive(http_url):
            raise Exception(
                f"[A·H3] 云端实例已终止或不可达 (SSH/HTTP 断开), 主动结束等待 "
                f"task_id={task_id}（不再傻等 {TIMEOUT}s）。如需重试请重新发起任务。")
        try:
            hist = get_history(http_url, pid, mode)
            entry = hist.get(pid) or {}
            if entry.get("status", {}).get("status_str") == "error":
                msgs = entry["status"].get("messages", [])
                err = json.dumps(msgs[-1], ensure_ascii=False)[:500] if msgs else "未知"
                raise Exception(f"[A·H3] 云端采样失败 task_id={task_id}: {err}")
        except Exception as e:
            if "云端采样失败" in str(e):
                raise
            last_err = e
            continue
        try:
            ready, _c = probe_output(http_url, cloud_basename + ".pt", mode)
            if ready:
                break
        except Exception as e:
            last_err = e
            continue
        # —— 云端日志实时跟随：增量 tail 打印到本地终端 ——
        if time.time() - _last_log_t >= 10:
            _last_log_t = time.time()
            try:
                if _log_path is None:
                    _log_path = _resolve_cloud_log_path()
                new_text, _last_line = _follow_cloud_log(_log_path, _last_line)
                if new_text and new_text.strip():
                    print(f"[云端日志] >>> {_log_path}")
                    for _ln in new_text.splitlines():
                        print(f"  {_ln}")
            except Exception as _e:
                print(f"[云端日志] 跟随异常: {_e}")
    else:
        raise Exception(f"[A·H3] 超时 ({TIMEOUT}s) task_id={task_id}, 最后异常: {last_err}")

    local_pt = os.path.join(cloud_temp_dir(), cloud_basename + ".pt")
    try:
        _sftp_download(f"{_cloud_output_dir()}{cloud_basename}.pt", local_pt)
    except Exception as e:
        print(f"[A·H3] SFTP 下载失败 ({e}), 回退 /view")
        import requests
        # 隧道本地地址强制不走系统代理(见 cloud_transport._NO_PROXY 说明)
        r = requests.get(view_url, timeout=60, proxies={"http": None, "https": None})
        if r.status_code != 200:
            raise Exception(f"[A·H3] /view 下载失败 (status {r.status_code}) task_id={task_id}")
        with open(local_pt, "wb") as f:
            f.write(r.content)
    size = _format_bytes(os.path.getsize(local_pt))
    _send_cloud_progress(task_id, f"[H3] 云端采样完成, 已下载结果 ({size}) → 本地 VAE 解码中…")
    return local_pt


def _rebuild_av_latent(local_pt, task_id):
    """下载 .pt → 反序列化为 LATENT (AV latent) → 校验并返回 dict"""
    if local_pt is None:
        raise Exception(f"[A·H3] 云端 task_id={task_id} 结果 .pt 未取到（超时或云端未回传）")
    cloud = torch.load(local_pt, map_location="cpu", weights_only=False)
    samples = cloud.get("samples")
    if samples is None:
        raise Exception(f"[A·H3] 云端 task_id={task_id} 返回的 samples 为空")
    if isinstance(samples, comfy.nested_tensor.NestedTensor):
        pass
    elif isinstance(samples, tuple) and len(samples) == 2:
        pass
    else:
        raise Exception(f"[A·H3] 云端 task_id={task_id} 返回 samples 形态非法: {type(samples)}")
    print(f"[A·H3] 带回 denoised AV latent task_id={task_id}")
    return {"samples": samples}


def _unwrap_av_samples(samples):
    """把 C 回传的 samples 规整成 VAEDecode 可接收的 LATENT {"samples": NestedTensor/tuple}"""
    if samples is None:
        return None
    if isinstance(samples, dict):
        inner = samples.get("samples")
        if inner is not None:
            return {"samples": inner}
    return {"samples": samples}


# ---------------------------------------------------------------------------
# 云端 H3 采样链拼装：以云端工作流为权威（本地只注入媒体 + 运行参数）
# ---------------------------------------------------------------------------
# 云端 H3 采样管线的唯一权威文件（二采 + SAGE + FirstBlockCache + latent upscaler
# 全由它定义）。本地节点只负责把加密 latent/cond 的 .pt 路径(管道=task_id) 与
# A 节点运行参数注入进去，绝不自己拼采样链。
CLOUD_H3_WF_REMOTE = "/root/autodl-tmp/ComfyUI/user/default/workflows/MiniMax H3全能参考工作流.json"

def _fetch_cloud_h3_template(http_url, mode):
    """SFTP 拉取云端 H3 工作流（UI 格式）到本地缓存，返回 dict。"""
    ensure_tunnel()
    local = os.path.join(cloud_temp_dir(), "h3_cloud_workflow.ui.json")
    TUNNEL.sftp_get(CLOUD_H3_WF_REMOTE, local)
    with open(local, "r", encoding="utf-8") as f:
        return json.load(f)

def _ui_to_api(wf):
    """纯结构 UI→API 转换（不依赖节点类）:
    - 有 link 的输入 → [origin_node_id, origin_slot]（途经 BYPASS(mode==4) 节点自动透传）
    - 带 widget 且无 link 的输入 → 取 widgets_values 下一个值
    - 无 widget 且无 link 的输入 → 跳过（ComfyUI 不会传，如 CloudLoadInputsH3.model）
    - ComfyUI 节点 mode 语义（mode: 0=ALWAYS, 1=NEVER, 4=BYPASS）:
        * mode==1(NEVER/画布停用): 整节点剔除, 下游依赖其输出的输入按「未连接」处理
          （真·禁用, 该链本就不该跑）。
        * mode==4(BYPASS/旁路): 节点本身不执行, 但其输入原样**透传**给下游——输出槽 k
          等价于「输入槽 k 的来源」。这是 ComfyUI 的「绕过」语义, 也是修掉
          SplitSigmas 399「Required input is missing: sigmas」的关键: 之前把 BYPASS
          当 NEVER 整棵删掉, 导致 399 的 sigmas 来源悬空。
    """
    nodes = {int(n["id"]): n for n in wf.get("nodes", [])}
    link_map = {l[0]: l for l in wf.get("links", [])}
    # NEVER(mode==1): 真·剔除; BYPASS(mode==4): 不执行但透传。两者本身都不入图。
    never = {nid for nid, n in nodes.items() if int(n.get("mode", 0)) == 1}
    bypass = {nid for nid, n in nodes.items() if int(n.get("mode", 0)) == 4}

    # BYPASS 透传映射: 旁路节点 N 的「输出槽 k 的有效来源」= N 的输入槽 k 的来源。
    # 槽位索引对齐 ComfyUI 的 bypass 重连（输入槽 k ↔ 输出槽 k）。
    bypass_out_remap = {}  # {nid: {out_slot: (src_node, src_slot)}}
    for nid in bypass:
        n = nodes[nid]
        remap = {}
        for jin, inp in enumerate(n.get("inputs", [])):
            link = inp.get("link")
            if link is not None and link in link_map:
                l = link_map[link]
                remap[jin] = (int(l[1]), l[2])  # 输入槽 jin 的来源 (来源节点, 来源槽)
        if remap:
            bypass_out_remap[nid] = remap

    def _resolve_origin(link):
        """给定一条 link, 返回最终应作为来源的 [node_id, slot]; 若来源被 NEVER 或无透传则 None。"""
        cur_node, cur_slot = int(link[1]), link[2]
        seen = set()
        while True:
            if cur_node in seen:
                return None  # 防环
            seen.add(cur_node)
            if cur_node in never:
                return None  # 来源是 NEVER(真·停用) → 无输出
            if cur_node in bypass:
                nxt = bypass_out_remap.get(cur_node, {}).get(cur_slot)
                if nxt is None:
                    return None  # BYPASS 且该输出槽无对应输入来源 → 无法透传(悬空)
                cur_node, cur_slot = nxt
                continue  # 可能级联到另一个 BYPASS 节点
            return [str(cur_node), cur_slot]

    api = {}
    for nid, n in nodes.items():
        if nid in never or nid in bypass:
            continue  # NEVER / BYPASS 自身都不执行, 不入图
        ct = n.get("type")
        inputs = {}
        widgets = n.get("widgets_values") or []
        wi = 0
        for inp in n.get("inputs", []):
            name = inp["name"]
            link = inp.get("link")
            if link is not None and link in link_map:
                origin = _resolve_origin(link_map[link])
                if origin is None:
                    continue  # 来源被 NEVER / 无透传 → 该输入未连接
                inputs[name] = origin
            elif inp.get("widget") is not None:
                if wi < len(widgets):
                    inputs[name] = widgets[wi]
                    wi += 1
            # 无 widget 且无 link → 跳过
        api[str(nid)] = {"class_type": ct, "inputs": inputs}
    return api

def _build_h3_cloud_wf(task_id, params, http_url, mode):
    """以云端工作流为权威拼 H3 采样链：拉模板 → 转 API → 注入协同点。

    协同点（Y·参数协同）：
      CloudLoadInputsH3.管道 / CloudSendBackH3.task_id ← task_id（媒体 .pt 对齐）
      RandomNoise.noise_seed ← 种子
      BasicScheduler.{scheduler,steps,denoise} ← 调度器/步骤/降噪
      KSamplerSelect.sampler_name ← 采样器
      SplitSigmas.step ← 切分步数（A 节点滑块, 不绑死二采: 单采工作流无此节点时自动忽略）
        stage1 在此 sigma 索引结束、stage2 接手在 upscaler 之后精炼；切分点须落在
        ExtendIntermediateSigmas 插入的中间 sigma(0.6~0.8)之下, 避免 stage2 回噪。
        置 0 或 >=总步数 会让 stage2 空操作 → upscaled latent 不被精炼 → 出噪点。
      ExtendIntermediateSigmas 保持云端默认 steps=2（stage2 精炼步数，不覆盖）
    """
    wf = _fetch_cloud_h3_template(http_url, mode)
    api = _ui_to_api(wf)
    steps = int(params["steps"])
    for nid, node in api.items():
        ct = node["class_type"]
        inp = node["inputs"]
        if ct == "CloudLoadInputsH3":
            inp["管道"] = task_id
        elif ct == "CloudSendBackH3":
            inp["task_id"] = task_id
        elif ct == "RandomNoise":
            inp["noise_seed"] = params["seed"]
        elif ct == "BasicScheduler":
            inp["scheduler"] = params["scheduler"]
            inp["steps"] = steps
            inp["denoise"] = params["denoise"]
        elif ct == "KSamplerSelect":
            inp["sampler_name"] = params["sampler_name"]
        elif ct == "SplitSigmas":
            # 切分步数由 A 节点滑块直接给定(不绑死二采比例): stage1 在此索引结束、stage2 接手精炼。
            # 单采工作流(无 SplitSigmas 节点)不会进此分支 → 滑块被自动忽略, A 节点通用适配一/二采。
            # ⚠️ 置 0 或 >=总步数 会让 sigmas2 为空 → stage2 空操作 → upscaled latent 不被精炼 → 出噪点。
            # 切分点须落在 ExtendIntermediateSigmas 插入的中间 sigma(0.6~0.8)之下, 避免 stage2 回噪。
            inp["step"] = params["split_step"]
    return api


# ---------------------------------------------------------------------------
# A 节点主体（插入式：接 positive/latent）
# ---------------------------------------------------------------------------
class CloudPipeAsyncH3:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "positive": ("CONDITIONING",),
                "latent": ("LATENT",),
                "种子": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF,
                              "control_after_generate": True}),
            },
            "optional": {
                "步骤": ("INT", {"default": 20, "min": 1, "max": 10000}),
                "采样器": (["res_multistep", "euler", "dpmpp_2m", "dpmpp_3m_sde"], {"default": "res_multistep"}),
                "调度器": (["beta", "simple", "normal", "karras"], {"default": "beta"}),
                "降噪": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
                "切分步数": ("INT", {"default": 6, "min": 0, "max": 10000,
                                "tooltip": "二采切分点: stage1 在此 sigma 索引结束、stage2 接手精炼。"
                                           "置 0 或 >=总步数 会让 stage2 空操作(出噪点)。单采工作流(无 SplitSigmas 节点)忽略此项。"}),
            },
        }

    RETURN_TYPES = ("LATENT", "STRING")
    RETURN_NAMES = ("LATENT", "结果")
    OUTPUT_IS_LIST = (False, False)
    OUTPUT_NODE = True
    FUNCTION = "send_and_wait"
    CATEGORY = "自定义脚本/☁️ 云端/本机用"

    def send_and_wait(self, positive, latent, 种子,
                       步骤=20, 采样器="res_multistep", 调度器="beta",
                       降噪=1.0, 切分步数=6):
        if isinstance(种子, (list, tuple)):
            种子 = 种子[0]

        # 打包 payload：直接把原生节点产出的 cond/latent 上传（媒体不出本地）
        payload = {
            "positive": positive,
            "latent": latent,
        }

        # 隧道连接
        url = ensure_tunnel()
        mode, mode_cn, http_url = detect_mode(url)

        task_id = uuid.uuid4().hex[:10]
        local_pt = os.path.join(cloud_temp_dir(), f"h3_init_{task_id}.pt")
        torch.save(payload, local_pt)
        _upload_h3(local_pt, task_id)
        print(f"[A·H3] 已上传 cond/latent (task_id={task_id}, 连接={mode_cn})")

        # 拼云端采样链并提交轮询
        params = {
            "steps": int(步骤),
            "sampler_name": 采样器,
            "scheduler": 调度器,
            "denoise": float(降噪),
            "split_step": int(切分步数),
            "seed": int(种子),
        }
        cloud_wf = _build_h3_cloud_wf(task_id, params, http_url, mode)
        wf = {}
        keymap = {}
        for rel, node in cloud_wf.items():
            gk = f"{task_id}_{rel}"
            keymap[rel] = gk
            wf[gk] = {"class_type": node["class_type"], "inputs": dict(node["inputs"])}
        for rel, node in cloud_wf.items():
            gk = keymap[rel]
            inp = wf[gk]["inputs"]
            for k, v in list(inp.items()):
                if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] in keymap:
                    inp[k] = [keymap[v[0]], v[1]]

        local_out = _submit_and_wait_h3(task_id, wf, http_url, mode, mode_cn, params)
        av_latent = _rebuild_av_latent(local_out, task_id)
        av_latent = _unwrap_av_samples(av_latent.get("samples"))

        summary = (f"H3 云端采样完成 (task_id={task_id}, 连接={mode_cn}, "
                   f"步数={步骤})")
        _notify("H3 云端采样", summary, kind="success")
        return (av_latent, summary)


NODE_CLASS = CloudPipeAsyncH3
