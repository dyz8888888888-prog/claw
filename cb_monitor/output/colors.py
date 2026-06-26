"""
终端颜色工具 - ANSI颜色码
"""

# ANSI颜色码
RESET = '\033[0m'
BOLD = '\033[1m'
DIM = '\033[2m'

# 前景色
RED = '\033[91m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
MAGENTA = '\033[95m'
CYAN = '\033[96m'
WHITE = '\033[97m'
GRAY = '\033[90m'

# 背景色
BG_RED = '\033[101m'
BG_GREEN = '\033[102m'
BG_YELLOW = '\033[103m'
BG_BLUE = '\033[104m'

# 信号等级颜色
LEVEL_COLORS = {
    'S': RED,
    'A': YELLOW,
    'B': CYAN,
    'C': MAGENTA,
    'D': GRAY,
}

# 涨跌颜色
def pct_color(value: float) -> str:
    """根据涨跌幅返回颜色"""
    if value > 5:
        return RED
    elif value > 2:
        return BOLD + RED
    elif value > 0:
        return RED
    elif value == 0:
        return WHITE
    elif value > -3:
        return GREEN
    elif value > -5:
        return BOLD + GREEN
    else:
        return GREEN


def color_text(text: str, color: str, bold: bool = False) -> str:
    """带颜色的文本"""
    prefix = BOLD if bold else ''
    return f"{prefix}{color}{text}{RESET}"


def colored_pct(value: float) -> str:
    """带颜色的涨跌幅"""
    color = pct_color(value)
    sign = '+' if value > 0 else ''
    return color_text(f"{sign}{value:.2f}%", color)


def colored_level(level: str) -> str:
    """带颜色的信号等级"""
    color = LEVEL_COLORS.get(level, WHITE)
    return color_text(f"[{level}]", color, bold=True)


def colored_trade(price: float, change_pct: float) -> str:
    """带颜色的价格"""
    color = pct_color(change_pct)
    return color_text(f"{price:.2f}", color)
