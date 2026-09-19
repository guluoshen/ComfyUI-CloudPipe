# core/cloud_comfy_op.py
"""
CloudComfyOp —— 云端运维节点（合并版：单节点）

可见 widget（用户填/操作的）:
  历史模板 / 模板名 / SSH主机 / SSH端口 / SSH用户 / SSH密码
  连接并检测 / 保存模板 / 删除模板(选) / 删除(执行)
  拉云端日志 / 停止云端任务(发送云端 /interrupt 优雅取消, 不杀进程/不关实例)
  状态(STRING 输出，可接文本节点查看详情)

隐藏字段（自动探测填入，存全局+config，不显示）:
  云端HTTP地址 / 云端安装目录

设计要点:
  - 连接参数唯一来源 = 本节点 6 个 SSH widget；任何改动 debounce 同步到全局
    cloud_shared.CLOUD_CONN（供 A 节点 / 启动 / 检测读取）。隐藏字段(HTTP/安装目录)
    由「连接并检测」自动探测写入全局与模板，不暴露给用户。
  - 「连接并检测」合并了原「自动探测」+「检测连接」：SSH 连 → 远端拿安装目录/端口/GPU
    → 自动存为 host:port 模板 → 检测 HTTP 200 → 返回 connected 结构化结果（前端据此
    把节点边框/状态框标红或标绿）。
  - 删除模板带确认框（前端 confirm），删「删除模板」下拉选中的（非当前使用中的给二次确认）。
  - 启动脚本全绝对路径 + 整段 base64（见 _build_start_script / op_start），修复
    「点启动拉不起服务」根因（远端 $PATH 不含 miniconda python + 本地 $PATH 被插值成
    Windows 路径传到云端）。
  - 密码隐藏由前端 JS 在 onNodeCreated + onConfigure 双重强制 inputEl.type=password，
    本处仅保留 password:True 作为兜底声明。

后端 API（单路由 /custom_script/cloud_comfy_op）:
  op ∈ {连接并检测, 保存模板, 删除, 切换, 拉云端日志, 停止云端任务}
  另 GET /custom_script/cloud_comfy_profiles 供前端拉模板列表。
"""

import os
import json
import time
import base64
from urllib.parse import urlparse

from server import PromptServer
from aiohttp import web
import asyncio

# 路由注册守卫（V3.1 修复）：PromptServer.instance 未就绪时跳过注册，不炸包。
# _maybe_route(method, path) 返回一个装饰器：instance 就绪 → 真实注册；未就绪 → no-op 直接返回原函数。
def _maybe_route(method, path):
    inst = getattr(PromptServer, "instance", None)
    if inst is None:
        def _noop(fn):
            return fn
        return _noop
    return getattr(inst.routes, method)(path)

from .cloud_tunnel import TUNNEL, ensure_tunnel


# ---------------------------------------------------------------------------
# 加密存储 (连接参数持久化到本地, 重启自动回填)
# ---------------------------------------------------------------------------
try:
    from cryptography.fernet import Fernet
    _HAS_CRYPTO = True
except Exception:
    _HAS_CRYPTO = False

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# V3.2 目录重构后文件位于 core/cloud/ 下，插件根需上三级（core/cloud -> core -> 插件根）
_CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(_BASE_DIR)), "config")
_KEY_FILE = os.path.join(_CONFIG_DIR, ".cloud_key")
_CFG_FILE = os.path.join(_CONFIG_DIR, "cloud_comfy.json")

# 可见 SSH 字段
_SSH_KEYS = ("SSH主机", "SSH端口", "SSH用户", "SSH密码")
# 完整 6 连接键（含隐藏的 HTTP / 安装目录）
_CONNECT_KEYS = ("云端HTTP地址", "SSH主机", "SSH端口", "SSH用户", "SSH密码", "云端安装目录")

# 内置示例模板：首次加载即可见（端口为占位值，请改成你自己实例的 SSH 主机/端口）
_DEFAULT_PROFILES = {
    "示例实例": {
        "云端HTTP地址": "http://127.0.0.1:6006",
        "SSH主机": "connect.cqa1.seetacloud.com",
        "SSH端口": 12345,
        "SSH用户": "root",
        "SSH密码": "",
        "云端安装目录": "/root/autodl-tmp/ComfyUI",
    },
}
_DEFAULT_LAST = "示例实例"

# 最新一次操作结果（供 operate() 图执行时回传给「状态」输出，以便接文本节点查看）
_LAST_STATUS = "未操作"


def _ensure_dirs():
    os.makedirs(_CONFIG_DIR, exist_ok=True)


def _load_key():
    _ensure_dirs()
    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "rb") as f:
            return f.read()
    key = Fernet.generate_key() if _HAS_CRYPTO else os.urandom(32)
    with open(_KEY_FILE, "wb") as f:
        f.write(key)
    try:
        os.chmod(_KEY_FILE, 0o600)
    except Exception:
        pass
    return key


def _xor(data, key):
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _default_config():
    return {"profiles": {k: dict(v) for k, v in _DEFAULT_PROFILES.items()}, "last": _DEFAULT_LAST}


def load_config():
    if not os.path.exists(_CFG_FILE):
        return _default_config()
    try:
        key = _load_key()
        with open(_CFG_FILE, "rb") as f:
            blob = f.read()
        if _HAS_CRYPTO:
            plain = Fernet(key).decrypt(blob)
        else:
            plain = _xor(blob, key)
        d = json.loads(plain.decode("utf-8"))
        if "profiles" not in d:
            name = f"{d.get('SSH主机','host')}-{d.get('SSH端口','')}" or "default"
            d = {"profiles": {name: d}, "last": name}
        if not d.get("profiles"):
            d = _default_config()
        return d
    except Exception as e:
        print(f"[云端运维] 读配置失败, 用内置默认: {e}")
        return _default_config()


def save_config(cfg):
    _ensure_dirs()
    key = _load_key()
    data = json.dumps(cfg, ensure_ascii=False).encode("utf-8")
    blob = Fernet(key).encrypt(data) if _HAS_CRYPTO else _xor(data, key)
    with open(_CFG_FILE, "wb") as f:
        f.write(blob)
    try:
        os.chmod(_CFG_FILE, 0o600)
    except Exception:
        pass


def get_profiles():
    return load_config().get("profiles", {})


def get_profile_names():
    names = list(get_profiles().keys())
    return names if names else ["(新建)"]


def get_last():
    last = load_config().get("last")
    if last not in get_profiles():
        names = get_profile_names()
        last = names[0] if names else "(新建)"
    return last


def get_profile(name):
    return get_profiles().get(name)


def save_profile(name, vals):
    cfg = load_config()
    prof = {k: vals.get(k) for k in _CONNECT_KEYS}
    cfg["profiles"][name] = prof
    cfg["last"] = name
    save_config(cfg)
    return cfg


def delete_profile(name):
    cfg = load_config()
    cfg["profiles"].pop(name, None)
    if not cfg["profiles"]:
        cfg["profiles"] = {"(新建)": _default_config()["profiles"].get("(新建)", {
            "云端HTTP地址": "http://127.0.0.1:6006",
            "SSH主机": "", "SSH端口": 12345, "SSH用户": "root",
            "SSH密码": "", "云端安装目录": "/root/autodl-tmp/ComfyUI",
        })}
    if cfg.get("last") == name or cfg.get("last") not in cfg["profiles"]:
        cfg["last"] = next(iter(cfg["profiles"]))
    save_config(cfg)
    return cfg


# ---------------------------------------------------------------------------
# 远端操作 (paramiko)
# ---------------------------------------------------------------------------
def _parse_port(http_url, default=6006):
    try:
        p = urlparse(http_url).port
        return int(p) if p else default
    except Exception:
        return default


def _ssh_connect(host, port, user, pwd, timeout=20):
    """(已废弃) 原每次新建 paramiko SSHClient; 现统一走自建隧道 cloud_tunnel.TUNNEL。

    保留此名仅为兼容历史引用; 新代码请用 TUNNEL.ensure / TUNNEL.ssh_exec。
    """
    return TUNNEL.ensure(host, port, user, pwd, 6006)


def op_check(host, port, user, pwd):
    """探活: 经自建隧道本地端口请求云端 / , 返回消息。取代原 SSH 通道 curl。
    先 ensure_tunnel() 真正建/复用隧道(含存活探测 + 会话失效自愈重连),
    避免『缓存 URL 还在, 但底层 SSH 已掉』导致的混乱状态。"""
    try:
        url = ensure_tunnel()
    except Exception as e:
        return f"SSH 不通: {e}"
    try:
        import requests
        code = requests.get(f"{url}/", timeout=5).status_code
        if code == 200:
            return f"运行中 (隧道 {url} 响应 200)"
        return f"未运行 (隧道 {url} 响应 '{code}')"
    except Exception as e:
        return f"隧道连不通: {e}"


def _build_autostart_script(install_dir, port_num):
    """启动脚本 100% 由本地生成（内嵌代码），云端只临时落一份 /root/comfyui_autostart.sh 再执行
    → 换任意云端实例都无缝（云端零预置）。
    幂等：已在运行则 exit 0；pkill 清残留；GPU 双判据；全绝对路径不依赖远端 $PATH。"""
    py = "/root/miniconda3/bin/python"
    nohup = "/usr/bin/nohup"
    log = "/root/comfyui_auto.log"
    return (
        "#!/bin/bash\n"
        "# ComfyUI 启动脚本（内容由本地「云端运维」节点生成，云端无需预置）\n"
        f"cd '{install_dir}'\n"
        f"if curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{port_num}/ --max-time 3 | grep -q 200; then\n"
        "  exit 0\n"
        "fi\n"
        "pkill -f 'main.py --listen' 2>/dev/null || true\n"
        "sleep 1\n"
        "if timeout 3 bash -c '</dev/tcp/127.0.0.1/1080' 2>/dev/null; then\n"
        "  export http_proxy=http://127.0.0.1:1080 https_proxy=http://127.0.0.1:1080 all_proxy=http://127.0.0.1:1080\n"
        "else\n"
        "  unset http_proxy https_proxy all_proxy\n"
        "fi\n"
        f"if ls /dev/nvidia* >/dev/null 2>&1 && {py} -c \"import torch; assert torch.cuda.is_available()\" >/dev/null 2>&1; then\n"
        "  EXTRA=''\n"
        "else\n"
        "  EXTRA='--cpu'\n"
        "fi\n"
        f"exec {nohup} {py} main.py --listen 0.0.0.0 --port {port_num} $EXTRA > {log} 2>&1 &\n"
    )


def _deploy_and_run(install_dir, port_num):
    """本地生成脚本 → base64 重写云端 /root/comfyui_autostart.sh → setsid 执行。
    经自建隧道同一条 SSH 会话执行(取代原 ssh.exec_command); 绝不依赖 PATH / 不传本地环境变量。"""
    script = _build_autostart_script(install_dir, port_num)
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    TUNNEL.ssh_exec(
        f"echo {b64} | base64 -d > /root/comfyui_autostart.sh && chmod +x /root/comfyui_autostart.sh"
        " && setsid bash /root/comfyui_autostart.sh < /dev/null > /dev/null 2>&1 &")


def _http_code(port_num):
    """经自建隧道会话探云端 ComfyUI 端口(取代原 ssh.exec_command curl)。"""
    out, _ = TUNNEL.ssh_exec(
        f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{port_num}/ --max-time 5")
    return out.strip()


def op_start(http_url, host, port, user, pwd, install_dir):
    """启动云端 ComfyUI：本地生成幂等脚本 → 重写云端 autostart.sh → setsid 执行 → 轮询端口。
    全部经自建隧道同一条 SSH 会话。返回 (connected: bool, message: str)"""
    port_num = _parse_port(http_url)
    try:
        url = TUNNEL.http_url() or TUNNEL.ensure(host, port, user, pwd, port_num)
    except Exception as e:
        return False, f"SSH 不通: {e}"
    if _http_code(port_num) == "200":
        return True, f"已在运行 (端口 {port_num} 响应 200)，跳过启动"
    _deploy_and_run(install_dir, port_num)

    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(4)
        try:
            if _http_code(port_num) == "200":
                return True, f"启动成功 (端口 {port_num} 已响应)"
        except Exception:
            pass
    return (False, f"启动命令已发，但 180s 内端口未响应"
                   f"（可能仍在加载模型，稍后点「连接并检测」复查；日志见云端 /root/comfyui_auto.log）")


def op_restart(http_url, host, port, user, pwd, install_dir):
    try:
        TUNNEL.ensure(host, port, user, pwd, _parse_port(http_url))
    except Exception as e:
        return False, f"SSH 不通: {e}"
    TUNNEL.ssh_exec("pkill -f 'main.py --listen' || true")
    time.sleep(3)
    return op_start(http_url, host, port, user, pwd, install_dir)


def _local_pkg_dir():
    """返回本机 自定义脚本 包根目录(含 core/ nodes.py js/ ...)
    __file__ = .../自定义脚本/core/cloud_comfy_op.py → 向上两级到包根"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


def _remote_pkg_dir(install_dir):
    """探测云端 自定义脚本 包目录名(中文 或 英文 fallback)，不存在则按中文名创建。

    返回 (remote_dir, name) —— remote_dir 为正斜杠完整路径。
    优先沿用云端已存在的同名目录(避免新建一个云端加载不到的目录)；
    若都没有，按本机中文名创建(云端 ComfyUI 对 UTF-8 目录名支持正常)。
    """
    base = install_dir.rstrip("/") + "/custom_nodes"
    # 候选名(按优先级)：本机实际名 → 英文 fallback
    local_name = os.path.basename(_local_pkg_dir())
    candidates = [local_name, "自定义脚本", "custom_script", "custom_nodes_zh"]
    found = None
    for nm in candidates:
        out, _ = TUNNEL.ssh_exec(
            f"if [ -d '{base}/{nm}' ]; then echo EXISTS; else echo MISSING; fi")
        if "EXISTS" in out:
            found = nm
            break
    if found is None:
        found = local_name  # 创建本机同名目录
    remote_dir = f"{base}/{found}"
    # 确保存在
    TUNNEL.ssh_exec(f"mkdir -p '{remote_dir}'")
    return remote_dir, found


def op_sync_code(http_url, host, port, user, pwd, install_dir):
    """把本机 自定义脚本 包整目录递归 SFTP 到云端同名目录, 然后重启云端 ComfyUI。

    跳过: __pycache__ / .git / 临时文件(*.tmp, ~$*) —— 避免传字节码缓存与半写文件。
    完成后 pkill 重启云端, 让新代码生效。
    全部经自建隧道同一 SSH 会话(与运维节点其它 op 一致)。
    """
    try:
        TUNNEL.ensure(host, port, user, pwd, _parse_port(http_url))
    except Exception as e:
        return False, f"SSH 不通: {e}"

    local_pkg = _local_pkg_dir()
    remote_dir, remote_name = _remote_pkg_dir(install_dir)

    # 递归收集本地文件(跳过缓存/临时；密钥文件与配置目录绝不传云端, 违反口径 Z)
    skip_dirs = {"__pycache__", ".git", "node_modules", ".pytest_cache", "config"}
    skip_exts = (".tmp", ".pyc")
    skip_files = {".cloud_key", "cloud_comfy.json"}
    file_list = []
    for root, dirs, files in os.walk(local_pkg):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in files:
            if f in skip_files or f.endswith(skip_exts) or f.startswith("~$"):
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(full, local_pkg).replace("\\", "/")
            file_list.append((full, rel))

    total = len(file_list)
    if total == 0:
        return False, "本机 自定义脚本 目录没有可同步的文件"
    ok_cnt = 0
    fail_cnt = 0
    first_err = ""
    for full, rel in file_list:
        remote_path = f"{remote_dir}/{rel}"
        try:
            TUNNEL.sftp_put(full, remote_path)
            ok_cnt += 1
        except Exception as e:
            fail_cnt += 1
            if not first_err:
                first_err = f"{rel}: {e}"
    msg_sync = f"代码同步: 成功 {ok_cnt}/{total}" + (f" (失败 {fail_cnt}: {first_err})" if fail_cnt else "")
    if fail_cnt:
        return False, msg_sync + " —— 同步未完成，未重启云端"

    # 同步完成 → 重启云端让新代码生效
    restart_ok, restart_msg = op_restart(http_url, host, port, user, pwd, install_dir)
    kind = "success" if restart_ok else "error"
    full_msg = f"{msg_sync} | 云端已重启: {restart_msg} (云端目录: {remote_dir})"
    return restart_ok, full_msg


def op_test(http_url):
    from .cloud_pipe_sender import run_cloud_test
    return run_cloud_test(http_url)


def op_autodetect(host, port, user, pwd):
    """仅用 SSH 四件套，远端自动获取 安装目录 + ComfyUI端口(→HTTP地址) + GPU模式。
    全部经自建隧道同一条 SSH 会话(先建隧道拿会话, 探测真实端口, 必要时重建转发到真实端口)。"""
    res = {"ok": False, "云端安装目录": "", "云端HTTP地址": "", "gpu": "", "message": ""}
    try:
        # 先建隧道(默认转发 6006)拿到会话, 才能远端探测
        TUNNEL.ensure(host, port, user, pwd, 6006)
    except Exception as e:
        res["message"] = f"SSH 不通: {e}"
        return res
    o1, _ = TUNNEL.ssh_exec(
        "find /root -maxdepth 6 -name main.py -path '*ComfyUI*' 2>/dev/null | head -1")
    inst = o1.strip()
    if not inst:
        o1b, _ = TUNNEL.ssh_exec(
            "ls -d /root/autodl-tmp/ComfyUI /root/ComfyUI /root/comfyui 2>/dev/null | head -1")
        inst = o1b.strip()
    if inst:
        res["云端安装目录"] = os.path.dirname(inst)

    o2, _ = TUNNEL.ssh_exec(
        "ps aux | grep '[m]ain.py' | grep -oP '(?<=-\\-port )[0-9]+' | head -1")
    port_s = o2.strip()
    comfy_port = int(port_s) if port_s else 6006
    # 把云端真实监听端口写进全局(供 A 节点建隧道当远端转发目标, 与隧道本地地址隔离)
    from .cloud_shared import get_cloud_conn_full, set_cloud_conn
    _h, _p, _u, _pwd, _http, _inst = get_cloud_conn_full()
    set_cloud_conn(_h or host, _p or port, _u or user, _pwd or pwd, _http, _inst, cloud_port=comfy_port)
    # 若真实端口非默认 6006, 重建隧道转发到真实端口(保证 HTTP/WS 走对端口)
    if comfy_port != 6006:
        TUNNEL.stop(host, port)
        TUNNEL.ensure(host, port, user, pwd, comfy_port)
    else:
        # 确保隧道转发到 6006(远端目标用真实端口, 不用 http_url 的隧道本地端口)
        TUNNEL.stop(host, port)
        TUNNEL.ensure(host, port, user, pwd, comfy_port)
    # 用隧道本地的真实转发地址(动态端口), 不是云端 6006 —— 否则本地 6006 无监听时 GET 失败
    res["云端HTTP地址"] = TUNNEL.http_url() or f"http://127.0.0.1:{comfy_port}"

    # GPU 双判据（与启动脚本一致，防假设备：/dev/nvidia* 存在但 torch 无 CUDA 时报 CPU）
    gpu_check = (
        "if ls /dev/nvidia* >/dev/null 2>&1 && "
        "/root/miniconda3/bin/python -c \"import torch; assert torch.cuda.is_available()\" >/dev/null 2>&1; "
        "then echo GPU; else echo CPU; fi"
    )
    o3, _ = TUNNEL.ssh_exec(gpu_check)
    res["gpu"] = o3.strip()

    mode_warn = "（⚠️ 无 GPU，请去控制台切换为 GPU 模式）" if res["gpu"] == "CPU" else ""
    res["message"] = (f"探测完成：安装目录 {res['云端安装目录'] or '(未找到)'}，"
                      f"端口 {comfy_port}，{res['gpu']} 模式{mode_warn}")
    res["ok"] = True
    return res


def _comfyui_root():
    """回退到 ComfyUI 根目录(用于定位 temp/ 落盘日志)。
    __file__ = .../自定义脚本/core/cloud/comfy_op.py → 上四级到 ComfyUI 根。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))


def op_pull_log(host, port, user, pwd, http_url, install_dir, lines=200):
    """经自建隧道 SSH 拉云端 ComfyUI 运行日志（优先 /root/comfyui_auto.log，
    退而 {install_dir}/comfyui.log），存本地 {ComfyUI根}/temp/cloud_log_{ts}.txt。
    返回 (log_text, log_path_remote, local_file)。"""
    try:
        TUNNEL.ensure(host, port, user, pwd, _parse_port(http_url))
    except Exception as e:
        raise RuntimeError(f"SSH 不通: {e}")
    base = (install_dir or "/root/autodl-tmp/ComfyUI").rstrip("/")
    # 候选日志路径（按优先级）：autostart 重定向的 stdout 日志 > 安装目录下的 comfyui.log
    candidates = ["/root/comfyui_auto.log", f"{base}/comfyui.log"]
    found = None
    for cand in candidates:
        out, _ = TUNNEL.ssh_exec(
            f"if [ -s '{cand}' ]; then echo SIZE; else echo MISSING; fi")
        if "SIZE" in out:
            found = cand
            break
    if not found:
        # 兜底：列出安装目录下最新的几个 *.log
        out2, _ = TUNNEL.ssh_exec(f"ls -1t {base}/*.log 2>/dev/null | head -3")
        alts = [x for x in (out2 or "").strip().splitlines() if x]
        if alts:
            found = alts[0]
    if not found:
        raise RuntimeError("云端未找到 ComfyUI 运行日志（comfyui_auto.log / comfyui.log 均不存在）")
    tail, _ = TUNNEL.ssh_exec(f"tail -n {int(lines)} '{found}'")
    log_text = tail or ""
    # 本地落盘（temp/cloud_log_{ts}.txt），便于用户打开查看完整日志
    ts = time.strftime("%Y%m%d_%H%M%S")
    temp_dir = os.path.join(_comfyui_root(), "temp")
    os.makedirs(temp_dir, exist_ok=True)
    local_file = os.path.join(temp_dir, f"cloud_log_{ts}.txt")
    try:
        with open(local_file, "w", encoding="utf-8") as f:
            f.write(f"# 云端日志来源: {found}\n# 拉取时间: {ts}\n\n")
            f.write(log_text)
    except Exception as e:
        local_file = f"(本地落盘失败: {e})"
    return log_text, found, local_file


def op_interrupt(host, port, user, pwd, http_url):
    """向云端 ComfyUI POST /interrupt —— 优雅取消当前正在执行的任务（清空当前 prompt 的执行），
    不杀进程、不关实例。失败抛错由路由转成 error 状态。"""
    port_num = _parse_port(http_url)
    try:
        TUNNEL.ensure(host, port, user, pwd, port_num)
    except Exception as e:
        raise RuntimeError(f"SSH 不通: {e}")
    url = TUNNEL.http_url() or f"http://127.0.0.1:{port_num}"
    import requests
    try:
        r = requests.post(f"{url}/interrupt", timeout=10)
    except Exception as e:
        raise RuntimeError(f"无法访问云端 /interrupt: {e}")
    if r.status_code != 200:
        raise RuntimeError(f"云端 /interrupt 返回 HTTP {r.status_code}")
    return True


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------
class CloudComfyOp:
    @classmethod
    def INPUT_TYPES(cls):
        names = get_profile_names()
        last = get_last()
        if last not in names:
            last = names[0]
        return {
            "required": {},
            "optional": {
                "历史模板": (names, {"default": last}),
                "模板名": ("STRING", {"default": last, "multiline": False}),
                "SSH主机": ("STRING", {"default": "", "multiline": False}),
                "SSH密码": ("STRING", {"default": "", "multiline": False, "password": True}),
                "连接并检测": ("BOOLEAN", {"default": False}),
                "保存模板": ("BOOLEAN", {"default": False}),
                "删除模板": (names, {"default": names[0]}),
                "删除": ("BOOLEAN", {"default": False}),
                "拉云端日志": ("BOOLEAN", {"default": False}),
                "停止云端任务": ("BOOLEAN", {"default": False}),
                "状态": ("STRING", {"default": "未操作", "multiline": True}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("状态",)
    OUTPUT_NODE = True
    FUNCTION = "operate"
    CATEGORY = "自定义脚本/☁️ 云端/运维"

    def operate(self, 历史模板="", 模板名="", SSH主机="", SSH密码="",
                 连接并检测=False, 保存模板=False, 删除模板="", 删除=False,
                 拉云端日志=False, 停止云端任务=False,
                 状态="未操作"):
        # 图执行时静默同步连接信息到全局（端口/用户从全局/模板保留），不打断工作流。
        # 运维动作（连接并检测/重启/保存/删除）只在开关被前端勾选时由 route 触发，
        # 不会走到这里——这里只负责把「SSH主机/密码」喂给 ABC 等下游节点。
        from .cloud_shared import get_cloud_conn_full, set_cloud_conn
        h0, p0, u0, pw0, http0, inst0 = get_cloud_conn_full()
        host, port_hint, user_hint = _normalize_host(SSH主机)
        new_port = port_hint if port_hint is not None else p0
        new_user = user_hint if user_hint is not None else u0
        # 密码非空才覆盖，避免把全局密码清空
        new_pwd = SSH密码 if SSH密码 else pw0
        set_cloud_conn(host, new_port, new_user, new_pwd, http0, inst0)
        # 状态显示由前端在 POST 回调里直接写 widget，不靠工作流执行
        return (状态,)


# ---------------------------------------------------------------------------
# 连接参数解析（核心：密码以「前端输入真值」为准, 绝不静默回退到硬编码默认密码）
# ---------------------------------------------------------------------------
def _normalize_host(raw):
    """解析 SSH主机 框里各种写法, 返回 (host, port_hint, user_hint)。

    支持:
      - ssh -p 12345 root@connect.cqa1.seetacloud.com   (本次改造主推格式；12345 为占位端口)
      - ssh root@host -p 12345
      - connect.cqa1.seetacloud.com:12345               (旧 host:port 兼容)
      - root@connect.cqa1.seetacloud.com
      - connect.cqa1.seetacloud.com
    """
    import re
    s = (raw or "").strip()
    if not s:
        return "", None, None
    port_hint = None
    user_hint = None
    # 1) 提取 -p <port> / -P <port>（位置不限）
    m = re.search(r"-[pP]\s+(\d+)", s)
    if m:
        port_hint = int(m.group(1))
        s = (s[:m.start()] + s[m.end():]).strip()
    # 2) 提取 user@host
    m = re.search(r"([A-Za-z0-9._-]+)@([^\s]+)", s)
    if m:
        user_hint = m.group(1)
        s = m.group(2)
    else:
        # 去掉开头的 ssh 字样（如 "ssh -p 12345 ..." 已无端口/user 残留，只剩 host）
        s = re.sub(r"^\s*ssh\b", "", s, flags=re.I).strip()
        # 兼容 host:port
        if ":" in s:
            _h, _sep, _p = s.rpartition(":")
            if _sep and _p.isdigit():
                s = _h.strip()
                port_hint = port_hint or int(_p)
    return s.strip(), port_hint, user_hint


def _resolve_conn(vals):
    """从前端 vals 解析 (host, port, user, pwd, note)。

    密码: 以用户在前端「SSH密码」框输入的为准(显式字符串, 可能为空); 空串也显式返回,
          由调用方决定是否报错, 绝不静默回退到 _DEFAULT/全局里的其他实例密码。
    端口: host:port 显式 > 同主机历史模板(主机唯一时) > 全局当前值(仅记日志提示)。
          (注: cqa1 两实例主机同名, 端口必须显式带 :port 或选模板, 主机无法唯一定端口)
    """
    from .cloud_shared import get_cloud_conn_full
    host_raw = (vals.get("SSH主机", "") or "").strip()
    host, port_hint, user_hint = _normalize_host(host_raw)

    g_host, g_port, g_user, g_pwd, _, _ = get_cloud_conn_full()
    user = user_hint or g_user or "root"
    # 前端真值（用户输入 / 模板回填 / onConfigure 恢复）; 空串也保留, 不吞
    pwd = (vals.get("SSH密码", "") or "").strip()
    notes = []

    if port_hint is not None:
        port = port_hint
    else:
        # 同主机历史模板匹配：cqa1 两实例主机同名, 优先用「上次使用」的模板消解歧义
        last = get_last()
        matches = [(_n, _p) for _n, _p in get_profiles().items()
                   if (_p.get("SSH主机") or "").strip() == host]
        matched, matched_name = None, ""
        if len(matches) == 1:
            matched_name, matched = matches[0]
        elif len(matches) > 1:
            last_match = [(n, p) for n, p in matches if n == last]
            if last_match:
                matched_name, matched = last_match[0]
                notes.append(f"同主机多模板, 取上次使用的[{last}]")
            else:
                matched_name, matched = matches[0]
                notes.append("同主机多模板, 取首个(建议主机带 :port)")
        if matched and matched.get("SSH端口"):
            port = int(matched["SSH端口"])
            notes.append(f"端口取模板[{matched_name}]")
            if not pwd and matched.get("SSH密码"):
                pwd = matched["SSH密码"]
                notes.append("密码取模板")
        else:
            port = g_port or 12345
            notes.append("端口回退全局/默认(建议主机带 :port 或选模板)")

    if not pwd:
        notes.append("密码为空→需显式填写")
    return host, port, user, pwd, "；".join(notes)


# ---------------------------------------------------------------------------
# 路由（V3.1 修复：instance 未就绪时 _maybe_route 自动 no-op，不炸包）
# ---------------------------------------------------------------------------
@_maybe_route("get", "/custom_script/cloud_comfy_profiles")
async def cloud_comfy_profiles_route(request):
    cfg = load_config()
    profiles = cfg.get("profiles", {})
    return web.json_response({
        "names": list(profiles.keys()),
        "last": cfg.get("last"),
        "profiles": profiles,
    })


@_maybe_route("post", "/custom_script/cloud_comfy_op")
async def cloud_comfy_op_route(request):
    global _LAST_STATUS
    try:
        data = await request.json()
    except Exception:
        data = {}
    op = data.get("op")
    vals = data.get("vals", {}) or {}
    name = (vals.get("模板名", "") or "").strip() or get_last() or "default"

    from .cloud_shared import get_cloud_conn_full, set_cloud_conn
    from .cloud_pipe_sender import _notify

    try:
        loop = asyncio.get_running_loop()

        if op == "连接并检测":
            host, port, user, pwd, note = _resolve_conn(vals)
            # 诊断日志: 显示实际解析出的 host/port/user 与密码长度(绝不打印密码明文)
            print(f"[云端运维] 连接参数解析: host={host} ssh_port={port} user={user} "
                  f"密码长度={len(pwd or '')} 来源={note}")
            if not host:
                msg = "请先填写 SSH主机"
                kind = "error"; connected = False
            elif not pwd:
                msg = ("未提供 SSH密码：请在节点「SSH密码」框输入，或先选「历史模板」回填。"
                       "（密码仅本地加密存储，不会以明文出现在任何日志/状态里）")
                kind = "error"; connected = False
            else:
                print(f"[云端运维] 开始连接并检测: host={host}, ssh_port={port}, user={user}")
                detected = await loop.run_in_executor(None, op_autodetect, host, port, user, pwd)
                print(f"[云端运维] 探测结果: {detected}")
                http_url = detected.get("云端HTTP地址") or f"http://127.0.0.1:{port}"
                inst = detected.get("云端安装目录") or "/root/autodl-tmp/ComfyUI"
                # 自动存为 host:port 模板（换主机不串用旧值；端口=实际用到的 SSH 端口）
                tpl = f"{host}:{port}"
                prof = {"云端HTTP地址": http_url, "SSH主机": host, "SSH端口": port,
                        "SSH用户": user, "SSH密码": pwd, "云端安装目录": inst}
                save_profile(tpl, prof)
                set_cloud_conn(host, port, user, pwd, http_url, inst)
                chk = await loop.run_in_executor(None, op_check, host, port, user, pwd)
                print(f"[云端运维] 探活结果: {chk}")
                if "运行中" in chk:
                    connected = True
                    kind = "success"
                    msg = f"{detected.get('message','')} | {chk}"
                else:
                    # 未连接 → 自动执行启动（本地生成脚本重写云端 autostart.sh 并 setsid 执行）
                    print(f"[云端运维] 云端未运行, 尝试自动启动: {http_url}")
                    ok, start_msg = await loop.run_in_executor(
                        None, op_start, http_url, host, port, user, pwd, inst)
                    connected = ok
                    kind = "success" if ok else "error"
                    msg = f"{detected.get('message','')} | 未运行 → 自动启动: {start_msg}"
                    print(f"[云端运维] 启动结果: connected={connected}, {start_msg}")
                # ★ 数据往返确认：本地↔云端无损传输（需云端 CloudTestEcho 节点，无模型可跑）
                data_ok = None
                if connected:
                    try:
                        print(f"[云端运维] 开始数据往返测试: {http_url}")
                        data_msg = await loop.run_in_executor(None, op_test, http_url)
                        data_ok = "PASS" in data_msg
                        msg += f" | 数据往返: {data_msg}"
                        print(f"[云端运维] 数据往返结果: {data_msg}")
                    except Exception as e:
                        data_ok = False
                        msg += f" | 数据往返测试失败: {e}"
                        print(f"[云端运维] 数据往返测试失败: {e}")
            _LAST_STATUS = msg
            _notify("云端运维", "已连通" if connected else "未连通", kind)
            return web.json_response({"ok": True, "connected": connected,
                                      "data_ok": data_ok, "message": msg, "kind": kind})

        elif op == "保存模板":
            host, port, user, pwd, http_url, inst = get_cloud_conn_full()
            prof = {"云端HTTP地址": http_url, "SSH主机": host, "SSH端口": port,
                    "SSH用户": user, "SSH密码": pwd, "云端安装目录": inst}
            save_profile(name, prof)
            msg = f"已保存模板「{name}」"
            kind = "success"
            _LAST_STATUS = msg
            _notify("云端运维", "保存模板", kind)
            return web.json_response({"ok": True, "message": msg, "kind": kind, "name": name})

        elif op == "删除":
            del_name = (vals.get("删除模板", "") or "").strip() or name
            delete_profile(del_name)
            msg = f"已删除模板「{del_name}」"
            kind = "success"
            _LAST_STATUS = msg
            _notify("云端运维", "删除模板", kind)
            return web.json_response({"ok": True, "message": msg, "kind": kind, "name": del_name})

        elif op == "切换":
            prof = get_profile(name)
            if not prof:
                msg = f"模板「{name}」不存在"
                kind = "error"
            else:
                set_cloud_conn(prof.get("SSH主机", ""), prof.get("SSH端口", 12345),
                               prof.get("SSH用户", "root"), prof.get("SSH密码", ""),
                               prof.get("云端HTTP地址", "http://127.0.0.1:6006"),
                               prof.get("云端安装目录", "/root/autodl-tmp/ComfyUI"))
                cfg = load_config()
                cfg["last"] = name
                save_config(cfg)
                msg = f"已切换到模板「{name}」"
                kind = "success"
            _LAST_STATUS = msg
            _notify("云端运维", "切换模板", kind)
            return web.json_response({"ok": True, "message": msg, "kind": kind, "name": name})

        elif op == "拉云端日志":
            host, port, user, pwd, note = _resolve_conn(vals)
            if not host:
                msg = "请先填写 SSH主机（或选历史模板回填）"
                kind = "error"
            elif not pwd:
                msg = "未提供 SSH密码：请在「SSH密码」框输入，或先选历史模板回填"
                kind = "error"
            else:
                g_host, g_port, g_user, g_pwd, http_url, inst = get_cloud_conn_full()
                try:
                    log_text, log_path, local_file = await loop.run_in_executor(
                        None, op_pull_log, host, port, user, pwd, http_url, inst)
                    nlines = len([x for x in log_text.splitlines() if x.strip()])
                    msg = (f"已拉取云端日志（来源 {log_path}，{nlines} 行）；"
                           f"本地存于 {local_file}")
                    kind = "success"
                    _LAST_STATUS = msg
                    _notify("云端运维", "拉云端日志", kind)
                    return web.json_response({"ok": True, "message": msg, "kind": kind,
                                              "log": log_text, "log_file": local_file})
                except Exception as e:
                    msg = f"拉取云端日志失败: {e}"
                    kind = "error"
            _LAST_STATUS = msg
            _notify("云端运维", "拉云端日志", kind)
            return web.json_response({"ok": (kind == "success"), "message": msg, "kind": kind})

        elif op == "停止云端任务":
            host, port, user, pwd, note = _resolve_conn(vals)
            if not host:
                msg = "请先填写 SSH主机（或选历史模板回填）"
                kind = "error"
            else:
                g_host, g_port, g_user, g_pwd, http_url, inst = get_cloud_conn_full()
                try:
                    await loop.run_in_executor(
                        None, op_interrupt, host, port, user, pwd, http_url)
                    msg = "已向云端发送 /interrupt：优雅取消当前任务（不杀进程、不关实例）"
                    kind = "success"
                except Exception as e:
                    msg = f"停止云端任务失败: {e}"
                    kind = "error"
            _LAST_STATUS = msg
            _notify("云端运维", "停止云端任务", kind)
            return web.json_response({"ok": (kind == "success"), "message": msg, "kind": kind})

        else:
            msg = f"未知操作: {op}"
            kind = "error"
            return web.json_response({"ok": False, "message": msg, "kind": kind})

    except Exception as e:  # noqa: BLE001
        print(f"[云端运维] 路由处理异常: {type(e).__name__}: {e}")
        return web.json_response({"ok": False, "message": str(e)})


NODE_CLASS = CloudComfyOp
