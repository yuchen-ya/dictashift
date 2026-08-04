"""文本语种检测：CJK 字符比例启发式，为划词翻译判定翻译方向。

只做中/英二分类（detect_direction）：明显含中文 -> "zh2en"，否则 -> "en2zh"。
不引入第三方依赖。
"""


# CJK 统一表意文字范围（码点闭区间）：基本区 + 扩展 A + 兼容表意文字。
# 不含日文假名/韩文谚文——本工具只区分中/英，
# 纯假名/谚文文本会落入 "en2zh"（非中文即按英文方向处理）。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x4E00, 0x9FFF),   # CJK 统一表意文字（基本区）
    (0x3400, 0x4DBF),   # CJK 统一表意文字扩展 A
    (0xF900, 0xFAFF),   # CJK 兼容表意文字
)

# 中文判定阈值 0.5（多数原则）：CJK 字符占有效字符比例超过一半即视为中文文本。
# 取多数而非 "含少量中文即判中"，是为了让夹个别中文专名的英文段落仍按英译中
# 处理；反之中文为主的混排文本（如 "今天我要翻译一段 English 文本"）稳定判中。
_CJK_RATIO_THRESHOLD = 0.5


def _is_cjk(ch: str) -> bool:
    """判断字符是否落在 CJK 统一表意文字范围内。"""
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def detect_direction(text: str) -> str:
    """检测文本应使用的翻译方向。

    规则：
    - 有效字符 = 字母（str.isalpha()：CJK 表意文字与拉丁字母均为字母，
      空白/数字/标点不计入，避免标点数字稀释比例——如 "你好！！！" 仍判中）。
    - CJK 字符数 > 0 且 CJK 占有效字符比例超过阈值（0.5）-> "zh2en"。
    - 其余情况 -> "en2zh"；空文本/纯数字标点没有有效字符，约定返回 "en2zh"。
    """
    letters = 0
    cjk = 0
    for ch in text:
        if not ch.isalpha():
            continue
        letters += 1
        if _is_cjk(ch):
            cjk += 1
    # cjk > 0 蕴含 letters >= cjk > 0，除法安全
    if cjk > 0 and cjk / letters > _CJK_RATIO_THRESHOLD:
        return "zh2en"
    return "en2zh"
