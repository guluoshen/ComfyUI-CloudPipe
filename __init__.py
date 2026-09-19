# ComfyUI-CloudPipe
# 云端协作采样自定义节点包（本机 A ↔ 云端 B/C + 运维节点）
#
# 目录结构:
#   __init__.py          —— 入口，部署前端 JS 到 ComfyUI 前端扩展目录
#   nodes.py             —— 节点注册中心
#   core/                —— 后端逻辑（隧道 / 传输 / ABC 节点 / 运维节点）
#   js/                  —— 前端扩展（运维节点 UI）

import os
import shutil

WEB_DIRECTORY = "./js"

# aki 版 ComfyUI 前端只加载 comfyui_frontend_package/static/extensions/ 下的扩展，
# 不会自动加载 custom node 的 WEB_DIRECTORY，因此需把 js 同步过去才能生效。
def _deploy_frontend_js():
    try:
        import comfyui_frontend_package
        frontend_ext = os.path.join(
            os.path.dirname(comfyui_frontend_package.__file__),
            "static", "extensions", "cloudpipe",
        )
        os.makedirs(frontend_ext, exist_ok=True)
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js", "cloud_comfy_op.js")
        dst = os.path.join(frontend_ext, "cloud_comfy_op.js")
        if os.path.exists(src):
            shutil.copy2(src, dst)
            print("[CloudPipe] 已部署运维节点前端到前端扩展目录")
    except Exception as _e:
        print(f"[CloudPipe] 警告: 跳过前端部署，原因: {_e}")


_deploy_frontend_js()


from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS  # noqa: E402

__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS', 'WEB_DIRECTORY']
