# core/cloud_transport.py
"""
云端 HTTP 交互 —— 统一走自管 SSH 隧道（cloud_tunnel.TUNNEL）

背景: A/B/C 云端协作需要与云端 ComfyUI 做 HTTP 交互 (/prompt 提交、/history 轮询、
/view 取回传文件、WS 进度监听)。这些全部走【自管 SSH 隧道】的本地转发端口
(如 http://127.0.0.1:12345, 由 cloud_tunnel 动态分配, 转发到云端 127.0.0.1:6006)。

为什么不再有"SSH 通道"分支:
  - 原设计隧道连不通时, 退化为"云端容器内 curl 提交 /prompt + SFTP 直读 output/"。
  - 现已改为: 隧道由本节点代码自己建立(paramiko 持久会话 + 本地端口转发), 不存在
    "连不通就退化"的情况; 隧道建不起来 = SSH 连不上云 = 直接报错(不依赖控制台 6006)。
  - 文件传输(SFTP 上传/下载)也复用同一条隧道会话(cloud_tunnel.TUNNEL.sftp_*),
    不再开独立 SSH 连接。

对外接口:
  detect_mode(http_url=None)              → ("tunnel", "隧道", 本地url)
  post_prompt(http_url, prompt_obj, mode, client_id)  → prompt_id
  get_history(http_url, pid, mode)        → dict
  probe_output(http_url, filename, mode)  → (ready: bool, content: bytes|None)
  fetch_output(http_url, filename, local_path, mode)
"""

import json

import requests

from .cloud_tunnel import get_http_url


# 隧道本地转发地址(127.0.0.1:动态端口)强制不走任何 HTTP 代理。
# 原因: 本机常设 HTTP_PROXY/HTTPS_PROXY=127.0.0.1:7890(Clash 等), 虽然 NO_PROXY
# 含 127.0.0.1, 但 requests 对『带端口的 127.0.0.1:53435』在部分版本下漏匹配,
# 会把本地隧道流量错误送到 7890 → 上传/下载卡死或报 502。显式传 None 最稳,
# 不依赖环境 NO_PROXY 的脆弱匹配。SSH/SFTP 走 paramiko 原生 TCP, 本就不受此影响。
_NO_PROXY = {"http": None, "https": None}


def _url(http_url):
    """优先用调用方传入的 url, 否则回落到自建隧道的本地 url"""
    return http_url or get_http_url()


def detect_mode(http_url=None, timeout=3):
    """统一自管隧道: 永远返回 tunnel 模式 + 本地转发 url。

    调用方拿到 (mode, mode_cn, url), mode 恒为 "tunnel", url 为隧道本地地址。
    若隧道尚未建立, get_http_url() 会自动 ensure(读全局连接建隧道)。
    """
    url = _url(http_url)
    return "tunnel", "隧道", url


def post_prompt(http_url, prompt_obj, mode, client_id=""):
    """提交 /prompt → 返回 prompt_id。经自建隧道本地端口。"""
    url = _url(http_url)
    body = dict(prompt_obj)
    body["client_id"] = client_id
    resp = requests.post(f"{url}/prompt", json=body, timeout=30, proxies=_NO_PROXY).json()
    if "prompt_id" not in resp:
        raise Exception(f"云端拒绝: {json.dumps(resp, ensure_ascii=False)[:300]}")
    return resp["prompt_id"]


def get_history(http_url, pid, mode):
    """查 /history/{pid} → dict。经自建隧道本地端口。"""
    url = _url(http_url)
    return requests.get(f"{url}/history/{pid}", timeout=10, proxies=_NO_PROXY).json()


def probe_output(http_url, filename, mode):
    """判断 output 文件就绪。隧道: /view 200 且 >1024B。返回 (ready, content|None)。"""
    url = _url(http_url)
    r = requests.get(f"{url}/view?filename={filename}&type=output", timeout=10, proxies=_NO_PROXY)
    return (r.status_code == 200 and len(r.content) > 1024), (r.content if r.status_code == 200 else b"")


def fetch_output(http_url, filename, local_path, mode):
    """下载 output 文件。隧道: /view 写本地。"""
    url = _url(http_url)
    r = requests.get(f"{url}/view?filename={filename}&type=output", timeout=60, proxies=_NO_PROXY)
    if r.status_code != 200:
        raise Exception(f"/view 下载失败 status {r.status_code}")
    with open(local_path, "wb") as f:
        f.write(r.content)
