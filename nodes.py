# nodes.py —— ComfyUI-CloudPipe 节点注册中心

from .core.cloud_pipe_sender import CloudPipeAsync
from .core.cloud_load_inputs import CloudLoadInputs
from .core.cloud_send_back import CloudSendBack
from .core.cloud_comfy_op import CloudComfyOp
from .core.cloud_test_echo import CloudTestEcho
from .core.cloud_pipe_sender_h3 import CloudPipeAsyncH3
from .core.cloud_load_inputs_h3 import CloudLoadInputsH3
from .core.cloud_send_back_h3 import CloudSendBackH3

NODE_CLASS_MAPPINGS = {
    # ---- 本机用（A：发+收，闭环）----
    "CloudPipeAsync": CloudPipeAsync,
    # ---- 云端用（B：接收+组装；C：回传；Echo：测试）----
    "CloudLoadInputs": CloudLoadInputs,
    "CloudSendBack": CloudSendBack,
    "CloudTestEcho": CloudTestEcho,
    # ---- H3 视频云端协作三件套（A/B/C）----
    "CloudPipeAsyncH3": CloudPipeAsyncH3,
    "CloudLoadInputsH3": CloudLoadInputsH3,
    "CloudSendBackH3": CloudSendBackH3,
    # ---- 运维（连接管理 + 模板）----
    "CloudComfyOp": CloudComfyOp,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "CloudPipeAsync": "🔄 A·云端协作(发+收)",
    "CloudLoadInputs": "📥 B·云端接收+加载+打包",
    "CloudSendBack": "📤 C·云端结果传回",
    "CloudTestEcho": "🔁 云端测试回传(Echo)",
    "CloudPipeAsyncH3": "🔄 A·H3云端视频协作",
    "CloudLoadInputsH3": "📥 B·H3云端接收+透传",
    "CloudSendBackH3": "📤 C·H3云端AV Latent回传",
    "CloudComfyOp": "☁️ 云端运维",
}

__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS']
