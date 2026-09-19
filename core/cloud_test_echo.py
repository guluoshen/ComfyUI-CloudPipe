# core/cloud_test_echo.py
"""
云端测试回传节点 —— CloudTestEcho (由本地「连接并检测 / 测试」内部调用, 不单独暴露)

设计目标（用户要求）:
  - 云端测试节点: 接收本地传来的管道数据, 原样传回
  - 不加载任何模型 → 不依赖 GPU, 无卡实例也能执行
  - 本地用它验证「本地 ↔ 云端」数据传递是否无损
  - 序列化格式与 A/B/C 一致: torch.save(.pt) 整对象 (忠实序列化整个 cond/latent)

行为:
  1. 从云端 input/test_in_<task_id>.pt 读取本地上传的管道数据 (torch.load)
  2. 原样序列化到云端 output/cloud_result_<task_id>.pipe.pt (torch.save)
  3. 输出 pass-through 字符串结果

注意: 本文件与云端实例运行版本保持同步 (.pt 协议)；本地 run_cloud_test 依赖此协议。
"""

import os

import torch
from folder_paths import get_input_directory, get_output_directory


CLOUD_INPUT_DIR = "/root/autodl-tmp/ComfyUI/input/"
CLOUD_OUTPUT_DIR = "/root/autodl-tmp/ComfyUI/output/"


class CloudTestEcho:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "管道": ("STRING", {"default": "", "multiline": False}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("结果",)
    FUNCTION = "echo"
    OUTPUT_NODE = True
    # 本地菜单隐藏：不设置 CATEGORY，云端通过 class_type=CloudTestEcho 调用仍可执行。

    def echo(self, 管道):
        task_id = str(管道).strip()
        if not task_id:
            raise Exception("[ECHO] 管道参数(任务ID)为空")

        pt_in = os.path.join(CLOUD_INPUT_DIR, f"test_in_{task_id}.pt")
        if not os.path.exists(pt_in):
            raise Exception(f"[ECHO] 云端未收到上传文件 test_in_{task_id}.pt（请确认本地 SFTP 上传成功）")

        pipe = torch.load(pt_in, map_location="cpu", weights_only=False)

        os.makedirs(CLOUD_OUTPUT_DIR, exist_ok=True)
        pt_out = os.path.join(CLOUD_OUTPUT_DIR, f"cloud_result_{task_id}.pipe.pt")
        torch.save(pipe, pt_out)

        return (f"ECHO_OK task_id={task_id}",)


NODE_CLASS = CloudTestEcho
