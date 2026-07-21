from pathlib import Path
import json
import logging
import numpy as np
import onnxruntime as ort

logger = logging.getLogger(__name__)

class SenseVoiceEncoder:
    def __init__(self, encoder_path: str, providers: list):
        # 1. 资源路径
        self.model_path = encoder_path # 记录路径用于 TRT 缓存
        encoder_path = Path(encoder_path)

        # 2. 初始化会话 (职责下放：稳健的 Provider 选择逻辑)

        # 3. 初始化会话 (职责下放：稳健的 Provider 选择逻辑)
        session_opts = ort.SessionOptions()
        session_opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        session_opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
        session_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        logger.debug(f"[Encoder] 正在初始化 ONNX 会话 (Provider: {providers})...")
        self.session = ort.InferenceSession(str(encoder_path), providers=providers, sess_options=session_opts)

        # 3. 从 Metadata 加载内部配置 (配置全内置模式)
        meta = self.session.get_modelmeta().custom_metadata_map
        if "lid_dict" in meta:
            self.config = {
                "lid_dict": json.loads(meta["lid_dict"]),
                "textnorm_dict": json.loads(meta["textnorm_dict"]),
                "emo_dict": json.loads(meta["emo_dict"]),
                "input_size": int(meta.get("input_size", 560)),
                "output_size": int(meta.get("output_size", 512))
            }
        else:
            print("[Encoder] 警告：在 ONNX 中未找到元数据，将回退至硬编码配置。")
            self.config = {} # 或设置默认值

        # 4. 精度适配 (检测模型是 FP32 还是 FP16)
        in_type = self.session.get_inputs()[0].type
        self.input_dtype = np.float16 if 'float16' in in_type else np.float32
        logger.debug(f"[Encoder] 精度检测成功: {self.input_dtype}")

    def construct_prompt(self, lid="auto", itn=True):
        """构造 4 个 Prompt Token ID"""
        lid_dict = self.config.get("lid_dict", {})
        itn_dict = self.config.get("textnorm_dict", {})

        lid_idx = lid_dict.get(lid, 0)
        itn_str = "withitn" if itn else "woitn"
        itn_idx = itn_dict.get(itn_str, 14)

        # 核心逻辑镜像 official: Language(1) -> Event_Emo(2) -> Style(1)
        # 现在只返回索引，Gather 操作在 ONNX 内部完成
        prompt_ids = np.array([lid_idx, 1, 2, itn_idx], dtype=np.int64)
        return prompt_ids[np.newaxis, :] # (1, 4)

    def release(self):
        if hasattr(self, 'session') and self.session is not None:
            del self.session
            self.session = None

    def forward(self, lfr_feat, lid="zh", itn=True):
        """
        执行 Encoder 推理
        返回: enc_out (1, T+4, 512)
        """
        # 1. 构造 Prompt ID 序列
        prompt_ids = self.construct_prompt(lid=lid, itn=itn)

        T_valid = lfr_feat.shape[0]

        # 动态轴推理 (非 DML 模式或长度已超过固定值)
        mask = np.ones((1, T_valid), dtype=self.input_dtype)
        enc_out = self.session.run(None, {
            "speech_feat": lfr_feat[np.newaxis, ...].astype(self.input_dtype),
            "mask": mask,
            "prompt_ids": prompt_ids
        })[0]
        return enc_out
