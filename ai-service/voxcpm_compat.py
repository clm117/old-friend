"""
voxcpm 2.0.2 注意力掩码形状兼容层

背景：
voxcpm 2.0.2 的 `MiniCPMAttention.forward_step` 先生成 1D 掩码
`attn_mask = torch.arange(kv_len) <= position_id`，再直接传给
`torch.nn.functional.scaled_dot_product_attention`。新版 torch（如 2.15+cpu）
的 SDPA 融合内核要求掩码至少为 4D 可广播形状，遇到 1D 会抛
`IndexError: Dimension out of range (expected to be in range of [-1, 0], but got -2)`，
导致 CPU 默认路径无法合成。

处理：
在加载模型前，把该方法源码中的掩码表达式补成 `(1, 1, 1, kv_len)`
（与 voxcpm 后续版本一致）。只改这一处形状，注意力数值语义不变；
若已安装版本本身是 4D 则直接跳过，不做任何改动。

为什么不用锁 torch 版本：
生产镜像的 torch 由基础镜像提供，仓库无法约束；voxcpm 声明 torch>=2.5.0，
降级需逐个版本验证且影响 image 内其它依赖，故选择不改变依赖的本地方案。
"""
import inspect
import logging
import re
import textwrap

logger = logging.getLogger("yuban.voxcpm_compat")

_MASK_PATTERN = re.compile(
    r"^(?P<indent>[ \t]*)(?P<stmt>attn_mask\s*=\s*[^\n#]*<=\s*[^\n#]*?)$",
    re.MULTILINE,
)
_MASK_4D = ".view(1, 1, 1, -1)"


def apply():
    """为 voxcpm 的 forward_step 补齐 4D 掩码；返回是否处于已兼容状态"""
    try:
        from voxcpm.modules.minicpm4 import model as _model
    except Exception as e:  # 未安装 voxcpm 时不影响其它功能
        logger.warning("voxcpm 兼容层跳过：%s", e)
        return False
    cls = getattr(_model, "MiniCPMAttention", None)
    if cls is None or not hasattr(cls, "forward_step"):
        logger.warning("voxcpm 兼容层跳过：未找到 MiniCPMAttention.forward_step")
        return False
    if getattr(cls.forward_step, "_yuban_mask_4d", False):
        return True
    try:
        src = inspect.getsource(cls.forward_step)
    except (OSError, TypeError) as e:
        logger.warning("voxcpm 兼容层跳过：源码不可读 %s", e)
        return False
    if _MASK_4D in src:
        # 已安装版本本身已是 4D 掩码，无需处理
        cls.forward_step._yuban_mask_4d = True
        return True
    patched, count = _MASK_PATTERN.subn(
        lambda m: f"{m.group('indent')}{m.group('stmt')}{_MASK_4D}", src, count=1
    )
    if count != 1:
        logger.warning("voxcpm 兼容层跳过：未匹配到 1D attn_mask 语句")
        return False
    namespace = dict(_model.__dict__)
    exec(compile(textwrap.dedent(patched), "<voxcpm_compat>", "exec"), namespace)
    fn = namespace.get("forward_step")
    if not callable(fn):
        logger.warning("voxcpm 兼容层失败：重写后缺少 forward_step")
        return False
    fn._yuban_mask_4d = True
    cls.forward_step = fn
    logger.info("voxcpm 兼容层已生效：forward_step 的 attn_mask 已补齐 4D 形状")
    return True
