# core/cloud_shared.py
# 云端连接参数全局共享：CloudConnection(连接节点) 写入，CloudComfyManager(运维节点) 与
# CloudPipeAsync(A 节点) 读取。
# 这样 A 节点 / 运维节点 都不暴露连接 widget，但用连接节点填的连接（先设连接节点 → 再运维/采样）。

CLOUD_CONN = {
    "host": "",
    "port": 0,
    "user": "",
    "pwd": "",
    "http_url": "",   # 本地走 SSH 隧道直连云端 ComfyUI 的地址 (如 http://127.0.0.1:6006)
    "inst": "",       # 云端 ComfyUI 安装目录
    "cloud_port": 6006,  # 云端 ComfyUI 真实监听端口(运维探测确认, 默认 6006);
                         # 与 http_url 的「隧道本地端口」是两码事, 切勿混用
}

# 回落默认值 (仅端口/用户/主机作为提示; 密码绝不硬编码明文 —— 密码只来自前端输入或加密模板,
# 避免「静默用别的实例密码去连当前实例」这类隐蔽错误)
# 端口为占位值：请在前端「SSH主机」里填你自己实例的端口（同主机多实例靠端口区分）
_DEFAULT = {
    "host": "connect.cqa1.seetacloud.com",
    "port": 12345,
    "user": "root",
    "pwd": "",
    "http_url": "http://127.0.0.1:6006",
    "inst": "/root/autodl-tmp/ComfyUI",
}


def set_cloud_conn(host, port, user, pwd, http_url="", inst="", cloud_port=6006):
    """连接节点 / 运维节点 / 同步接口 写入当前连接。空值回落默认。

    注意: http_url 是「隧道本地转发地址」(动态端口, 如 http://127.0.0.1:53435),
    仅供排查展示; 云端 ComfyUI 真实监听端口单独存 cloud_port(默认 6006),
    用作自建隧道的「远端转发目标」。两者不能混——A 节点建隧道必须转发到 cloud_port,
    绝不能用 http_url 解析出的隧道本地端口当远端目标(否则转发到已死的探测端口→10061)。
    """
    CLOUD_CONN["host"] = str(host)
    CLOUD_CONN["port"] = int(port)
    CLOUD_CONN["user"] = user
    CLOUD_CONN["pwd"] = pwd
    CLOUD_CONN["http_url"] = http_url or _DEFAULT["http_url"]
    CLOUD_CONN["inst"] = inst or _DEFAULT["inst"]
    if cloud_port:
        CLOUD_CONN["cloud_port"] = int(cloud_port)


def set_cloud_http_url(url):
    """回填自建隧道的本地 http_url(如 http://127.0.0.1:12345), 供排查与一致性。"""
    CLOUD_CONN["http_url"] = url


def get_inst_dir():
    """返回云端 ComfyUI 安装目录(正斜杠), 供拼 input/ output/ 远端路径。"""
    if CLOUD_CONN.get("inst"):
        return CLOUD_CONN["inst"]
    return _DEFAULT["inst"]


def get_cloud_conn():
    """返回 (host, port, user, pwd)。优先全局，回落默认。供 SFTP 使用。"""
    if CLOUD_CONN.get("host"):
        return (CLOUD_CONN["host"], CLOUD_CONN["port"], CLOUD_CONN["user"], CLOUD_CONN["pwd"])
    return (_DEFAULT["host"], _DEFAULT["port"], _DEFAULT["user"], _DEFAULT["pwd"])


def get_cloud_conn_full():
    """返回 (host, port, user, pwd, http_url, inst)。供运维节点 / A 节点使用。"""
    if CLOUD_CONN.get("host"):
        return (CLOUD_CONN["host"], CLOUD_CONN["port"], CLOUD_CONN["user"], CLOUD_CONN["pwd"],
                CLOUD_CONN["http_url"], CLOUD_CONN["inst"])
    return (_DEFAULT["host"], _DEFAULT["port"], _DEFAULT["user"], _DEFAULT["pwd"],
            _DEFAULT["http_url"], _DEFAULT["inst"])


def get_cloud_port():
    """返回云端 ComfyUI 真实监听端口(默认 6006)。A 节点建隧道用它当远端转发目标,
    绝不能用 http_url 解析的隧道本地端口。"""
    return int(CLOUD_CONN.get("cloud_port") or _DEFAULT.get("cloud_port") or 6006)
