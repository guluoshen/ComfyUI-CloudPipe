# core/cloud_pipe_batch_sender.py
"""
CloudPipeBatchAsync（A 节点底层，被 CloudPipeAsync 委托调用，本地闭环发+收）

  - 输入管道是单条 pipe（系统默认，不拆批）。
  - 无模式概念，无并发: 单条 pipe 原样发云端采样，结果原样返回。

复用核心：
  - _save_cond / _save_init / _load_pipe_from_local / _sftp_upload / _sftp_download
    全部来自 cloud_pipe_sender（单条逻辑零改动复用，避免漂移）。
  - 云端 workflow 拼装（UNETLoader/CLIPLoader/loraStack/CR Apply LoRA Stack/
    CloudLoadInputs/easy fullkSampler/CloudSendBack）与单条完全一致，只是 task_id 不同。
"""

import os
import time
import json
import uuid

import torch
import comfy.samplers as comfy_samplers

# 复用单条发送模块的全部 helper（上传/下载/轮询/序列化）
from .cloud_pipe_sender import (
    _save_cond, _save_init, _load_pipe_from_local,
    _sftp_upload, _sftp_download,
    _unwrap_samples,
    _cloud_input_dir, _cloud_output_dir,
    cloud_temp_dir,
    _notify,
    CLOUD_UNET, CLOUD_CLIP, CLOUD_LORA,
    TIMEOUT, POLL_INTERVAL,
)
from .cloud_tunnel import ensure_tunnel
from .cloud_shared import get_cloud_conn_full
from .cloud_transport import detect_mode, post_prompt, get_history, probe_output


def _build_one_cloud_wf(task_id, params):
    """拼一条云端采样链（与 CloudPipeAsync 单条完全一致，只是 task_id 不同）。

    返回该链用到的节点 dict（键是相对名，外层负责加前缀避免冲突）。
    """
    chain = {
        "unet": {"class_type": "UNETLoader", "inputs": {
            "unet_name": CLOUD_UNET, "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": CLOUD_CLIP, "type": "qwen_image", "device": "default"}},
        "lorastack": {"class_type": "easy loraStack", "inputs": {
            "toggle": True, "mode": "simple", "num_loras": 1,
            "lora_1_name": CLOUD_LORA, "lora_1_strength": 1.0}},
        "applylora": {"class_type": "CR Apply LoRA Stack", "inputs": {
            "model": ["unet", 0], "clip": ["clip", 0], "lora_stack": ["lorastack", 0]}},
        "load": {"class_type": "CloudLoadInputs", "inputs": {
            "model": ["applylora", 0], "clip": ["applylora", 1], "管道": task_id}},
        "sampler": {"class_type": "easy fullkSampler", "inputs": {
            "pipe": ["load", 0],
            **params,
            "link_id": 0, "save_prefix": "ComfyUI"}},
        "sendback": {"class_type": "CloudSendBack", "inputs": {
            "管道": ["sampler", 0], "task_id": task_id}},
    }
    return chain


def _upload_one(管道, task_id, 种子):
    """上传单条管道的三个 .pt 到云端 input/，返回本地临时文件路径（用于诊断/清理）"""
    local_input = cloud_temp_dir()
    local_pos = os.path.join(local_input, f"pos_cond_{task_id}.pt")
    local_neg = os.path.join(local_input, f"neg_cond_{task_id}.pt")
    local_lat = os.path.join(local_input, f"cloud_init_{task_id}.pt")
    pos = 管道["positive"]
    neg = 管道["negative"]
    samples = 管道["samples"]
    pos_size = _save_cond(pos, local_pos)
    neg_size = _save_cond(neg, local_neg)
    lat_size = _save_init(samples, 种子, 管道.get("loader_settings"), local_lat)
    # 3 个文件并发 SFTP 上传
    from concurrent.futures import ThreadPoolExecutor as _TP
    with _TP(max_workers=3) as ex:
        futs = [
            ex.submit(_sftp_upload, local_pos, f"{_cloud_input_dir()}pos_cond_{task_id}.pt"),
            ex.submit(_sftp_upload, local_neg, f"{_cloud_input_dir()}neg_cond_{task_id}.pt"),
            ex.submit(_sftp_upload, local_lat, f"{_cloud_input_dir()}cloud_init_{task_id}.pt"),
        ]
        for f in futs:
            f.result()
    return {
        "pos": local_pos, "neg": local_neg, "lat": local_lat,
        "pos_size": pos_size, "neg_size": neg_size, "lat_size": lat_size,
    }


def _submit_and_wait_one(task_id, cloud_wf, http_url, mode, mode_cn, params_summary):
    """提交单条（或单批里的一条链）并轮询直到 cloud_result_<task_id>.pipe.pt 就绪。

    返回本地下载的 .pt 路径。
    """
    client_id = f"cloudBatch_{task_id}"
    pid = post_prompt(http_url, {"prompt": cloud_wf, "client_id": client_id}, mode, client_id)
    print(f"[A·批量] 任务已提交 (pid={pid}, task_id={task_id}, 模式={mode_cn})")

    cloud_basename = f"cloud_result_{task_id}.pipe"
    view_url = f"{http_url}/view?filename={cloud_basename}.pt&type=output"
    deadline = time.time() + TIMEOUT
    last_err = None
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            hist = get_history(http_url, pid, mode)
            entry = hist.get(pid) or {}
            if entry.get("status", {}).get("status_str") == "error":
                msgs = entry["status"].get("messages", [])
                err = json.dumps(msgs[-1], ensure_ascii=False)[:500] if msgs else "未知"
                raise Exception(f"[A·批量] 云端采样失败 task_id={task_id}: {err}")
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
    else:
        raise Exception(f"[A·批量] 超时 ({TIMEOUT}s) task_id={task_id}, 最后异常: {last_err}")

    local_pt = os.path.join(cloud_temp_dir(), cloud_basename + ".pt")
    try:
        _sftp_download(f"{_cloud_output_dir()}{cloud_basename}.pt", local_pt)
    except Exception as e:
        print(f"[A·批量] SFTP 下载失败 ({e}), 回退 /view")
        import requests
        r = requests.get(view_url, timeout=60)
        if r.status_code != 200:
            raise Exception(f"[A·批量] /view 下载失败 (status {r.status_code}) task_id={task_id}")
        with open(local_pt, "wb") as f:
            f.write(r.content)
    return local_pt


def _rebuild_pipe(原管道, local_pt, task_id):
    """下载 .pt → 反序列化为 PIPE_LINE → 用本地 model/clip/vae 重组

    samples 用 _unwrap_samples 兜底嵌套并强制校验 (防止 C 回传非法形态导致本机 VAEDecode 崩)
    """
    if local_pt is None:
        # 该条云端结果始终未就绪（超时/未回传），直接终止整图，
        # 不让 None 流到下游触发 'NoneType' object has no attribute 'get'。
        raise Exception(
            f"[A·批量] 云端 task_id={task_id} 的结果 .pt 未取到（超时或云端未回传），"
            f"无法重组 pipe，已终止整图。")
    cloud_pipe = _load_pipe_from_local(local_pt)
    new_pipe = dict(原管道)
    samples = _unwrap_samples(cloud_pipe.get("samples"))
    if samples is None or not isinstance(samples, dict) or not isinstance(samples.get("samples"), torch.Tensor):
        raise Exception(
            f"[A·批量] 云端 task_id={task_id} 返回的 samples 非法（维度异常/缺失），"
            f"无法重组 pipe。原因为云端采样未成功或回传结构损坏。")
    new_pipe["samples"] = samples
    new_pipe["positive"] = cloud_pipe.get("positive")
    new_pipe["negative"] = cloud_pipe.get("negative")
    new_pipe["seed"] = cloud_pipe.get("seed")
    new_pipe["loader_settings"] = cloud_pipe.get("loader_settings")
    print(f"[A·批量] 闭环完成 task_id={task_id}, 已带回新 samples (model/clip/vae 用本地)")
    return new_pipe


def _assert_all_pipes(results):
    """下游 easy pipeOut 会逐条调 pipe.get("model") 等；若 results 含非 dict
    （如 "ERROR:..." 字符串或 None），会触发 'str'/'NoneType' object has no
    attribute 'get'。此处做最后一道硬闸门：任何非 dict 直接整体 raise，
    错误信息标出第几条、什么类型，便于精准追溯，绝不把坏数据喂下游。"""
    for i, r in enumerate(results):
        if not isinstance(r, dict):
            raise Exception(
                f"[A·批量] 第 {i} 条结果不是合法 PIPE_LINE（类型={type(r).__name__}, "
                f"值={r if isinstance(r, str) else '...'}），已终止整图，"
                f"不把坏数据喂下游 easy pipeOut。")


class CloudPipeBatchAsync:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "管道": ("PIPE_LINE",),
            },
            "optional": {
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

    INPUT_IS_LIST = (False,
                     False, False, False, False, False, False, False)
    RETURN_TYPES = ("PIPE_LINE", "STRING")
    RETURN_NAMES = ("管道", "结果")
    OUTPUT_IS_LIST = (False, False)
    OUTPUT_NODE = True
    FUNCTION = "send_and_wait_batch"
    CATEGORY = "自定义脚本/☁️ 云端/本机用"

    def send_and_wait_batch(self, 管道,
                             步骤=30, CFG=4.0, 采样器="euler", 调度器="simple", 降噪=1.0,
                             种子=0, 图像输出="None"):
        # ---------- 0. 校验 ----------
        # 系统默认：管道是单条 pipe，不拆批。
        if not isinstance(管道, dict):
            raise Exception("[A] 输入 pipe 非 dict（系统默认仅支持单条 pipe）")
        pipes = [管道]
        if not pipes:
            raise Exception("[A·批量] 没有收到任何管道（上游 CR Prompt List / pipeIn 等打包节点无输出？）")
        for i, p in enumerate(pipes):
            if not isinstance(p, dict) or p.get("positive") is None \
                    or p.get("negative") is None or p.get("samples") is None:
                raise Exception(f"[A·批量] 第 {i} 个管道缺少 positive/negative/samples")

        # 云端连接（统一走自建隧道）
        # 重要: 隧道本地地址(url)只来自 ensure_tunnel() 动态分配的端口,
        # 绝不用 CLOUD_CONN["http_url"] 当隧道地址——那是运维探针写进去的云端内部地址
        # (如 http://127.0.0.1:53435), 会过期且被 stop() 关闭, 直接拿去 post /prompt 必 10061 拒绝。
        from .cloud_transport import detect_mode as _dm
        url = ensure_tunnel()                  # url = 本地转发端口(如 http://127.0.0.1:新端口), 唯一可信地址
        mode, mode_cn, http_url = _dm(url)    # http_url 此时 == url, 后续所有 HTTP/SFTP 都用它

        params = {
            "steps": int(步骤[0]) if isinstance(步骤, (list, tuple)) else int(步骤),
            "cfg": float(CFG[0]) if isinstance(CFG, (list, tuple)) else float(CFG),
            "sampler_name": 采样器[0] if isinstance(采样器, (list, tuple)) else 采样器,
            "scheduler": 调度器[0] if isinstance(调度器, (list, tuple)) else 调度器,
            "denoise": float(降噪[0]) if isinstance(降噪, (list, tuple)) else float(降噪),
            "seed": int(种子[0]) if isinstance(种子, (list, tuple)) else int(种子),
            "image_output": 图像输出[0] if isinstance(图像输出, (list, tuple)) else 图像输出,
        }

        N = len(pipes)
        base_tid = uuid.uuid4().hex[:10]
        task_ids = [f"{base_tid}_{i}" for i in range(N)]
        per_seeds = [params["seed"]] * N

        print(f"[A·批量] 共 {N} 条, 连接={mode_cn} (已去并发, 严格串行)")

        # ---------- 串行执行（去掉并发, 避免多线程抢 SSH 隧道导致认证失败 / Secsh channel 限流） ----------
        # 统一串行, 每条任务内部顺序完成 上传→提交→等→下载。
        def _build_wf(i):
            """拼第 i 条的单链 cloud_wf（带 tid 前缀全局键）"""
            tid = task_ids[i]
            wf_params = dict(params)
            wf_params["seed"] = int(per_seeds[i])
            chain = _build_one_cloud_wf(tid, wf_params)
            wf = {}
            keymap = {}
            for rel, node in chain.items():
                gk = f"{tid}_{rel}"
                keymap[rel] = gk
                wf[gk] = {"class_type": node["class_type"], "inputs": dict(node["inputs"])}
            for rel, node in chain.items():
                gk = keymap[rel]
                inp = wf[gk]["inputs"]
                for k, v in list(inp.items()):
                    if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] in keymap:
                        inp[k] = [keymap[v[0]], v[1]]
            return wf

        def _one_full(i):
            """单条完整流程: 上传 → 提交 /prompt → 轮询等待 → 下载 → 重组 pipe。严格串行。"""
            tid = task_ids[i]
            _upload_one(pipes[i], tid, per_seeds[i])
            wf = _build_wf(i)
            local_pt = _submit_and_wait_one(tid, wf, http_url, mode, mode_cn, params)
            return _rebuild_pipe(pipes[i], local_pt, tid)

        results = [None] * N
        for i in range(N):
            results[i] = _one_full(i)
        _assert_all_pipes(results)

        # 系统默认: 返回单条 pipe（不包 list）
        result = results[0] if results else None
        summary = (f"完成: 成功 {len(results)}/{N}"
                   f"（严格串行, 云端={mode_cn}）")
        return (result, summary)

