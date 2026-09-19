# core/cloud_tunnel.py
"""
CloudTunnel —— 自管 SSH 隧道（取代平台控制台隧道 6006 + 原独立 SFTP/SSH 通道）

背景:
  ABC 云端协作原本依赖两类外部通道:
    1) "隧道连接" = Seetacloud 控制台预先开好的 SSH 隧道, 把云端 6006 映射到本地 127.0.0.1:6006;
    2) "SSH 连接" = 隧道不通时, 每次临时新建 paramiko SSH: 云端容器内 curl 提交 /prompt,
       并用独立 paramiko SFTP 直读云端 output/ 取回传文件。
  两者都依赖"外部已开好的隧道"或"每次新建 SSH 会话", 既不统一也不稳定。

本模块用一条**自己建立的持久 SSH 会话**(paramiko Transport) 取代上述两类通道, 一会话三用:
    1) 本地端口转发: 在本地 127.0.0.1:<free_port> 监听, 经 direct-tcpip 转发到云端
       127.0.0.1:<comfy_port>(默认 6006) → 全部 HTTP(/prompt /history /view) 与 WS 进度
       都走这个本地端口(http_url), 等价于"自己新建的隧道", 不再依赖控制台 6006;
    2) SFTP 子系统: 上传 cond/latent .pt、下载结果 .pt, 复用同一条 SSH 会话;
    3) exec_command: 远端探测 / 启动 / 重启 / 探活, 复用同一条 SSH 会话(开 session channel)。

设计要点:
  - 按 (host, ssh_port) 缓存会话; 多次 ensure 复用同一条, 断开自动重连;
  - 单例 TUNNEL 供运维节点 / A 节点 / transport 共用, 保证全链路只有一条隧道;
  - 本地端口动态分配(bind 0), 避免与控制台 6006 冲突, 也支持多实例各自独立隧道;
  - 远端路径一律正斜杠(Windows os.path.join 会混反斜杠, 见历史坑位 12)。
"""

import os
import socket
import threading
from urllib.parse import urlparse


def _remote_comfy_port(http_url, default=6006):
    """从云端 HTTP 地址里取云端 ComfyUI 真实监听端口(即隧道要转发的远端端口)"""
    try:
        p = urlparse(http_url).port
        return int(p) if p else default
    except Exception:
        return default


class CloudTunnel:
    """自管 SSH 隧道管理器(线程安全, 按连接去重)"""

    def __init__(self):
        self._lock = threading.Lock()
        self._tunnels = {}        # (host, port) -> entry
        self._current_key = None  # 最近一次 ensure 的连接, 作为"当前激活"隧道

    # ------------------------------------------------------------------
    # 内部: 建立单条 SSH 会话 + 本地端口转发
    # ------------------------------------------------------------------
    def _connect(self, host, ssh_port, user, pwd):
        """建立一条已认证的 paramiko Transport(原始 Transport, 不走 SSHClient)"""
        import paramiko  # 延迟导入: 仅实际建隧道时才需要, 避免无 paramiko 的环境(纯云端 B/C 节点)被顶层 import 阻断整包加载
        try:
            transport = paramiko.Transport((host, int(ssh_port)))
            transport.start_client()
            transport.auth_password(username=user, password=pwd)
        except Exception as e:
            # 附加诊断: 连不上时顺便看看云端 ComfyUI 端口(默认 6006)是否通,
            # 帮助用户区分『实例已关机/挂起』还是『SSH 服务没开』
            comfy_ok = False
            try:
                probe = socket.create_connection((host, 6006), timeout=5)
                probe.close()
                comfy_ok = True
            except Exception:
                pass
            if comfy_ok:
                hint = (f"(提示: {host}:6006 可达, 但 SSH 端口 {ssh_port} 被拒绝, "
                        f"请检查 SSH 服务/端口/安全组)")
            else:
                hint = (f"(提示: SSH 端口 {ssh_port} 与 ComfyUI 端口 6006 均不可达, "
                        f"云端实例可能已关机/挂起, 请去 Seetacloud/AutoDL 控制台启动)")
            raise Exception(f"SSH 连接 {host}:{ssh_port} 失败 [{type(e).__name__}: {e}] {hint}") from e
        # 每 30s 发 SSH 级 keepalive, 防止云端实例空闲被服务端/网关踢掉连接
        # (这正是『隧道刚才还 200, 一跑数据往返就 SSH 不通』的常见根因)
        try:
            transport.set_keepalive(30)
        except Exception:
            pass
        return transport

    @staticmethod
    def _pipe(client, chan):
        """双向拷贝: 把一条本地 socket 与一条 direct-tcpip channel 接通"""
        def _copy(src, dst):
            try:
                while True:
                    buf = src.recv(65536)
                    if not buf:
                        break
                    dst.sendall(buf)
            except Exception:
                pass
            try:
                dst.close()
            except Exception:
                pass

        t1 = threading.Thread(target=_copy, args=(client, chan), daemon=True)
        t2 = threading.Thread(target=_copy, args=(chan, client), daemon=True)
        t1.start()
        t2.start()

    def _accept_loop(self, lsock, transport, remote_host, remote_port, key):
        """本地监听端口的接受循环: 每来一个连接就开一条 direct-tcpip 转发到云端

        新增退避: open_channel 被拒(典型为远端 6006 短暂未监听, 如云端 ComfyUI
        重启窗口)时, 不立刻死循环冲下一个连接, 而是短暂 sleep 再继续, 避免瞬间
        刷出几十个『Secsh channel N open FAILED: Connection refused』把 SSH 网关
        打满(此前 8/23 重启即触发此风暴)。网关本身未永久封禁, 远端恢复监听后自愈。
        """
        consecutive_fail = 0
        while True:
            try:
                client, _ = lsock.accept()
            except OSError:
                # 监听 socket 已关闭(隧道 stop) → 退出循环
                break
            try:
                chan = transport.open_channel(
                    "direct-tcpip", (remote_host, remote_port), client.getsockname())
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass
                # 退避: 连续失败累进 sleep(0.2→0.4→0.8...上限 3s), 成功则清零
                consecutive_fail += 1
                backoff = min(0.2 * (2 ** (consecutive_fail - 1)), 3.0)
                try:
                    import time as _t
                    _t.sleep(backoff)
                except Exception:
                    pass
                continue
            consecutive_fail = 0
            self._pipe(client, chan)

    # ------------------------------------------------------------------
    # 对外: 建立 / 复用隧道, 返回本地 http_url
    # ------------------------------------------------------------------
    def ensure(self, host, ssh_port, user, pwd, remote_comfy_port=6006, local_host="127.0.0.1"):
        """建立或复用一条自管隧道, 返回本地 http_url(如 http://127.0.0.1:12345)

        - 已存在且会话仍活跃 → 直接复用;
        - 已存在但会话断了 → 关旧建新;
        - 不存在 → 新建 SSH 会话 + 本地端口转发线程。
        """
        key = (host, int(ssh_port))
        with self._lock:
            entry = self._tunnels.get(key)
            if entry is not None and self._alive(entry):
                # 即使会话仍活, 也用当前传入的凭据刷新缓存副本,
                # 防止全局 CLOUD_CONN 在并发写入时密码被改/清空导致后续 _reconnect 拿到旧值
                entry["pwd"] = pwd
                self._current_key = key
                return entry["http_url"]
            if entry is not None:
                self._close_entry(entry)

            transport = self._connect(host, ssh_port, user, pwd)
            lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            lsock.bind((local_host, 0))
            lsock.listen(8)
            local_port = lsock.getsockname()[1]
            th = threading.Thread(
                target=self._accept_loop,
                args=(lsock, transport, "127.0.0.1", int(remote_comfy_port), key),
                daemon=True,
            )
            th.start()
            http_url = f"http://{local_host}:{local_port}"
            self._tunnels[key] = {
                "transport": transport,
                "lsock": lsock,
                "thread": th,
                "http_url": http_url,
                "local_port": local_port,
                "host": host,
                "ssh_port": int(ssh_port),
                "pwd": pwd,  # 缓存密码副本: 后续 _reconnect_current 用副本, 不依赖全局 CLOUD_CONN
            }
            self._current_key = key
            return http_url

    # ------------------------------------------------------------------
    # 存活检查 / 关闭
    # ------------------------------------------------------------------
    def _alive(self, entry):
        """更严的存活检查: is_active() 在连接被静默断开时可能返回 True(假阳性),
        因此额外开一个瞬态 session 通道验证通道确实可用, 避免复用『半死』隧道
        (否则会出现『op_check 还 200, 数据往返却 SSH 不通』的混乱状态)。"""
        try:
            t = entry["transport"]
            if not t.is_active():
                return False
            ch = t.open_session()
            ch.settimeout(5)
            ch.close()
            return True
        except Exception:
            return False

    def _close_entry(self, entry):
        try:
            entry["lsock"].close()
        except Exception:
            pass
        try:
            entry["transport"].close()
        except Exception:
            pass

    def stop(self, host=None, ssh_port=None):
        """关闭指定连接(不传则关当前激活的); 本地端口转发随之停止"""
        key = (host, int(ssh_port)) if (host is not None and ssh_port is not None) else self._current_key
        with self._lock:
            entry = self._tunnels.pop(key, None) if key is not None else None
            if entry:
                self._close_entry(entry)
            if self._current_key == key:
                self._current_key = None

    def stop_all(self):
        with self._lock:
            for entry in self._tunnels.values():
                self._close_entry(entry)
            self._tunnels.clear()
            self._current_key = None

    def _reconnect_current(self, max_retries=3):
        """强制断开并重建当前激活隧道(操作失败自愈用)。

        优先用已缓存的密码副本重建(避免全局 CLOUD_CONN 在并发时处于不一致状态,
        尤其避免『运维探针刚写好密码、采样批重连却读到空值』这类竞态);
        缓存缺失时再回落全局 CLOUD_CONN。向上抛错(诚实报错, 不掩盖)。

        退避重试: 单次重建失败不立即抛错, 按指数退避(1s→2s→4s, 上限 8s)重试至多
        max_retries 次。原因: 云端 ComfyUI 重启窗口(或 SSH 网关瞬时抖动)期间, 盲目
        立即重建会瞬间打出大量新会话/通道, 把网关打满(见 8/23 风暴); 退避后远端
        恢复即自愈。均失败才向上抛错。
        """
        import time as _t
        with self._lock:
            cur = self._tunnels.get(self._current_key) if self._current_key else None
            host = cur["host"] if cur else None
            port = cur["ssh_port"] if cur else None
            user = cur["user"] if cur else None
            pwd = cur.get("pwd") if cur else None
        if not (host and pwd):
            # 缓存缺失 → 回落全局
            from .cloud_shared import get_cloud_conn_full
            host, port, user, pwd, http_url, _ = get_cloud_conn_full()
        if not host:
            raise Exception("[隧道] 未配置云端连接, 无法自愈重连")
        from .cloud_shared import get_cloud_port
        remote_port = get_cloud_port()   # 远端目标用云端真实端口, 不混用隧道本地端口
        last_err = None
        for attempt in range(max_retries):
            if attempt > 0:
                backoff = min(1 * (2 ** (attempt - 1)), 8)
                try:
                    _t.sleep(backoff)
                except Exception:
                    pass
            try:
                self.stop()
                return self.ensure(host, port, user, pwd, remote_port)
            except Exception as e:
                last_err = e
                continue
        raise Exception(f"[隧道] 自愈重连 {max_retries} 次均失败: {last_err}")

    # ------------------------------------------------------------------
    # 当前激活隧道访问
    # ------------------------------------------------------------------
    def get_current(self):
        with self._lock:
            if self._current_key is None:
                return None
            return self._tunnels.get(self._current_key)

    def http_url(self):
        """返回当前激活隧道的本地 http_url; 未建立则返回 None"""
        e = self.get_current()
        return e["http_url"] if e else None

    # ------------------------------------------------------------------
    # 复用同一会话: 远端执行命令(取代原 _ssh_connect + exec_command)
    # ------------------------------------------------------------------
    def ssh_exec(self, cmd, timeout=60):
        """在隧道会话上开 session channel 执行命令, 返回 (stdout, stderr) 字符串。
        若会话已失效, 自动断开重建并重试一次(自愈瞬断)。"""
        try:
            return self._ssh_exec_impl(cmd, timeout)
        except Exception:
            self._reconnect_current()
            return self._ssh_exec_impl(cmd, timeout)

    def _ssh_exec_impl(self, cmd, timeout=60):
        e = self.get_current()
        if e is None:
            raise Exception("[隧道] 尚未建立, 请先 ensure()")
        transport = e["transport"]
        chan = transport.open_session()
        chan.settimeout(timeout)
        chan.exec_command(cmd)
        stdout = b""
        stderr = b""
        import socket as _sock
        while True:
            if chan.exit_status_ready():
                break
            try:
                if chan.recv_ready():
                    stdout += chan.recv(65536)
                if chan.recv_stderr_ready():
                    stderr += chan.recv_stderr(65536)
            except _sock.timeout:
                # 暂时没数据, 但命令可能还在跑 → 继续等退出状态
                continue
            except Exception:
                break
        # 排空残余输出
        while chan.recv_ready():
            stdout += chan.recv(65536)
        while chan.recv_stderr_ready():
            stderr += chan.recv_stderr(65536)
        try:
            chan.close()
        except Exception:
            pass
        return stdout.decode("utf-8", "ignore"), stderr.decode("utf-8", "ignore")

    # ------------------------------------------------------------------
    # 复用同一会话: SFTP(取代原 _sftp_upload / _sftp_download)
    # ------------------------------------------------------------------
    def sftp_put(self, local_path, remote_path):
        """本地文件 → 云端(自动建远程目录, 正斜杠路径)。会话失效自动重建重试一次。"""
        try:
            return self._sftp_put_impl(local_path, remote_path)
        except Exception:
            self._reconnect_current()
            return self._sftp_put_impl(local_path, remote_path)

    def _sftp_put_impl(self, local_path, remote_path):
        e = self.get_current()
        if e is None:
            raise Exception("[隧道] 尚未建立, 请先 ensure()")
        sftp = e["transport"].open_sftp_client()
        try:
            remote_dir = "/".join(remote_path.rstrip("/").split("/")[:-1])
            if remote_dir:
                try:
                    sftp.stat(remote_dir)
                except IOError:
                    parts = remote_dir.strip("/").split("/")
                    cur = ""
                    for p in parts:
                        cur += "/" + p
                        try:
                            sftp.stat(cur)
                        except IOError:
                            try:
                                sftp.mkdir(cur)
                            except IOError:
                                pass
            sftp.put(local_path, remote_path)
        finally:
            sftp.close()

    def sftp_get(self, remote_path, local_path):
        """云端文件 → 本地。会话失效自动重建重试一次。"""
        try:
            return self._sftp_get_impl(remote_path, local_path)
        except Exception:
            self._reconnect_current()
            return self._sftp_get_impl(remote_path, local_path)

    def _sftp_get_impl(self, remote_path, local_path):
        e = self.get_current()
        if e is None:
            raise Exception("[隧道] 尚未建立, 请先 ensure()")
        sftp = e["transport"].open_sftp_client()
        try:
            sftp.get(remote_path, local_path)
        finally:
            sftp.close()

    def sftp_stat_size(self, remote_path):
        """返回云端文件字节数, 不存在返回 None"""
        e = self.get_current()
        if e is None:
            raise Exception("[隧道] 尚未建立, 请先 ensure()")
        sftp = e["transport"].open_sftp_client()
        try:
            return sftp.stat(remote_path).st_size
        except Exception:
            return None
        finally:
            sftp.close()


# 单例: 运维节点 / A 节点 / transport 共用同一条隧道
TUNNEL = CloudTunnel()


# ---------------------------------------------------------------------------
# 便捷函数(读全局连接, 自动建/复用隧道)
# ---------------------------------------------------------------------------
def ensure_tunnel():
    """读全局 CLOUD_CONN, 建立/复用自管隧道, 返回本地 http_url, 并回填全局 http_url。

    远端转发目标端口用 get_cloud_port()(云端真实监听端口, 默认 6006),
    绝不用 CLOUD_CONN["http_url"] 解析——http_url 是隧道本地动态端口(如 53435),
    用它当远端目标会转发到已死的探测端口 → WinError 10061 积极拒绝。
    """
    from .cloud_shared import get_cloud_conn_full, set_cloud_http_url, get_cloud_port
    host, port, user, pwd, http_url, inst = get_cloud_conn_full()
    if not host:
        raise Exception("[隧道] 未配置云端连接(SSH主机为空), 请先在「云端运维」节点连接并检测")
    remote_port = get_cloud_port()   # 云端真实端口(6006), 与隧道本地端口隔离
    url = TUNNEL.ensure(host, port, user, pwd, remote_port)
    # 回填实际隧道本地地址, 便于排查与一致性(仅展示用, 不作远端目标)
    set_cloud_http_url(url)
    return url


def get_http_url():
    """返回当前激活隧道的本地 http_url(未建立则尝试建)"""
    url = TUNNEL.http_url()
    if url:
        return url
    return ensure_tunnel()
