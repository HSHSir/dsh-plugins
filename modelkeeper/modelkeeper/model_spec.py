#!/usr/bin/env python3
"""一个模型的「配置规格」：模型管家、界面、Agent 配置文件之间的唯一交换格式。

原来这套信息存在模型库里（`library.json`），每条模型记录上下文、图片、推理
等级、来源。库被取消后，这些值不再持久化，改为**导入时由用户在对话框里显式
选定**，装进 `ModelSpec` 直接写进目标配置。

这样反而更诚实：配置里出现的每个值，都对应某次用户按下的「确定」，
不存在「三周前从某个端点猜出来、后来端点换了模型也没人管」的历史遗留值。
"""

from __future__ import annotations

from dataclasses import dataclass

# 推理档位，顺序即从弱到强。``off`` 表示「关闭推理」，不是「不支持推理」——
# 后者在界面上用另一个概念表达（支持推理等级选 off）。
REASONING_LEVELS: tuple[str, ...] = ("off", "low", "medium", "high", "xhigh")

# 上下文长度选项。用 1024 进制而非 1000 进制：这是模型侧真实的 token 预算口径，
# 写 1000000 会让 DSH 认为窗口比实际略小，反而触发不必要的截断。
CONTEXT_OPTIONS: tuple[tuple[str, int], ...] = (
    ("32K", 32 * 1024),
    ("64K", 64 * 1024),
    ("128K", 128 * 1024),
    ("512K", 512 * 1024),
    ("1M", 1024 * 1024),
)

# 对话框的初始档位：改这里就等于改所有「导入 / 添加」的预填值。
DEFAULT_CONTEXT = 512 * 1024
DEFAULT_REASONING = "high"
DEFAULT_REASONING_MAX = "xhigh"

# 图片输入的初始勾选状态：新建条目默认按「支持图片」预勾选，纯文本模型
# 在对话框里取消一次即可。
DEFAULT_VISION = True

# 最大输出 token 的默认值：0 表示不写进配置，交给提供方自己的默认值。
# 探测到端点披露的 max_output_tokens 时会用它填默认值（见 gui_app._import_to）。
DEFAULT_MAX_TOKENS = 0

# 「首字超时、放弃测试」在表格里的显示值。
# 与真正的空值区分开：空表示没测过，``-`` 表示测过且断定不可用。
SKIP_LABEL = "-"

# 未测速时的占位符（与 SKIP_LABEL 不同的语义）。
UNSET_LABEL = "—"


def norm_id(model_id: str) -> str:
    """模型 ID 的比较键：去空白、忽略大小写。"""
    return (model_id or "").strip().lower()


def snap_context(value: int | None) -> int | None:
    """把配置里的原始数字对齐到最近的预设档位（容差 5%），否则原样返回。

    现网配置普遍用十进制写法（128000 / 512000），与预设的 131072 / 524288 只差
    2% 左右。若按字面显示会得到 "125K"，而对话框的上下文下拉只提供预设档位，
    于是「表格显示 125K、弹窗里却是空值」——用户看不出这其实是同一档。
    容差外的值（例如某模型真实写 600000）保持原样，不硬凑成档位。
    """
    if not value:
        return value
    label, size = min(CONTEXT_OPTIONS, key=lambda kv: abs(kv[1] - value))
    return size if abs(size - value) <= size * 0.05 else value


def context_label(value: int | None) -> str:
    """128K / 512K / 1M 这种紧凑写法；非预设值原样给数字。"""
    if not value:
        return ""
    for label, size in CONTEXT_OPTIONS:
        if value == size:
            return label
    snapped = snap_context(value)
    if snapped != value:
        return context_label(snapped)
    if value >= 1024 * 1024 and value % (1024 * 1024) == 0:
        return f"{value // (1024 * 1024)}M"
    if value % 1024 == 0 and value >= 1024:
        return f"{value // 1024}K"
    return str(value)


def parse_capacity(text: str) -> int:
    """'8192' / '8K' / '1M' → 整数；空串或认不出来给 0（= 不写进配置）。

    和 `context_label` 是反函数关系，口径同样是 1024 进制：写 8K 就是 8192，
    而不是 8000 —— 配置里两个字段用同一套换算，免得 8192 的模型被写成 8000。
    """
    raw = (text or "").strip().lower().replace(",", "").replace(" ", "")
    if not raw:
        return 0
    mult = 1
    if raw.endswith("k"):
        mult, raw = 1024, raw[:-1]
    elif raw.endswith("m"):
        mult, raw = 1024 * 1024, raw[:-1]
    if not raw.replace(".", "", 1).isdigit():
        return 0
    return int(float(raw) * mult)


def supported_levels(max_level: str) -> tuple[str, ...]:
    """支持到某一档为止的全部档位（含 off）。

    「支持推理等级 = medium」的含义是这个模型能选 off/low/medium，
    不能选 high/xhigh——写成截断的列表，而不是把 5 档全给出去。
    """
    if max_level not in REASONING_LEVELS:
        return REASONING_LEVELS
    return REASONING_LEVELS[: REASONING_LEVELS.index(max_level) + 1]


@dataclass
class ModelSpec:
    """写进 Agent 配置的模型参数。字段即对话框里的几项。"""

    context: int = DEFAULT_CONTEXT
    vision: bool = DEFAULT_VISION
    reasoning_max: str = DEFAULT_REASONING_MAX
    reasoning_level: str = DEFAULT_REASONING
    max_tokens: int = DEFAULT_MAX_TOKENS

    @property
    def reasoning(self) -> bool:
        return self.reasoning_max != "off"

    @property
    def levels(self) -> tuple[str, ...]:
        return supported_levels(self.reasoning_max)

    def efforts(self) -> dict[str, object]:
        """DSH `reasoningEfforts` 映射：档位名 -> 传给提供方的值。

        ``off: None`` 是 DSH 侧的真实写法——关闭推理在提供方就是不带参数。
        """
        return {lvl: (None if lvl == "off" else lvl) for lvl in self.levels}

    def default_level(self) -> str:
        """默认档必须落在支持范围内，否则 DSH 会拿一个不存在的档去请求。"""
        lv = self.reasoning_level if self.reasoning_level in self.levels else DEFAULT_REASONING
        return lv if lv in self.levels else self.levels[-1]

    def clone(self) -> "ModelSpec":
        return ModelSpec(self.context, self.vision, self.reasoning_max,
                         self.reasoning_level, self.max_tokens)
