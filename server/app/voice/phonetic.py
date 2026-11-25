"""中文方言与同音模糊匹配（零外部依赖）：服务实体消歧与「是不是店里已有商品」提醒。

设计原则
--------
- 小店主（尤其带平翘舌、n/l、f/h、前鼻音/后鼻音口音）使用微信端侧 ASR 时，
  常把「可乐」识别成「可了/口乐」，「牛奶」识别成「流奶」，「雪碧」识别成「学毕」。
- 本模块只负责计算字形/拼音归一化相似度与候选排序，**不擅自代替用户落账**：
  - 高确信且显著领先第 2 名时可辅助命中；
  - 接近新词 / 多候选接近时仅提供确认或「店里有 X，是它吗？」推荐。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable, Sequence, TypeVar

__all__ = [
    "canonical_phonetic",
    "find_similar",
    "fuzzy_score",
]

T = TypeVar("T")

#: 常见超市/小店商品名同音错字或方言字形归并表（单字 -> 归一化发音键）
_CHAR_PHONETIC: dict[str, str] = {
    # 可乐/雪碧/水饮
    "可": "ke", "口": "ke", "渴": "ke", "科": "ke",
    "乐": "le", "了": "le", "勒": "le",
    "雪": "xue", "学": "xue", "血": "xue", "薛": "xue",
    "碧": "bi", "毕": "bi", "比": "bi", "必": "bi", "壁": "bi", "鼻": "bi",
    "红": "hong", "宏": "hong", "洪": "hong", "鸿": "hong", "虹": "hong",
    "牛": "niu", "流": "niu", "刘": "niu", "留": "niu",
    "奶": "nai", "来": "nai", "耐": "nai",
    "矿": "kuang", "况": "kuang", "框": "kuang",
    "泉": "quan", "全": "quan", "权": "quan",
    "水": "shui", "谁": "shui", "税": "shui",
    "啤": "pi", "皮": "pi", "批": "pi", "疲": "pi",
    "酒": "jiu", "久": "jiu", "九": "jiu", "就": "jiu",
    "茶": "cha", "查": "cha", "差": "cha",
    "王": "wang", "旺": "wang", "忘": "wang", "往": "wang",
    "老": "lao", "脑": "lao", "劳": "lao",
    "吉": "ji", "及": "ji", "级": "ji", "极": "ji", "急": "ji",
    "娃": "wa", "瓦": "wa", "挖": "wa",
    "哈": "ha", "啥": "ha",
    "脉": "mai", "卖": "mai", "买": "mai", "迈": "mai",
    "动": "dong", "冻": "dong", "洞": "dong",
    "康": "kang", "抗": "kang", "扛": "kang",
    "师": "shi", "诗": "shi", "十": "shi", "实": "shi", "食": "shi", "时": "shi",
    "傅": "fu", "父": "fu", "付": "fu", "副": "fu", "富": "fu", "胡": "fu", "湖": "fu",
    # 粮油/调味/日杂
    "香": "xiang", "箱": "xiang", "想": "xiang", "乡": "xiang", "响": "xiang",
    "烟": "yan", "盐": "yan", "严": "yan", "颜": "yan",
    "花": "hua", "发": "hua", "华": "hua", "滑": "hua",
    "生": "sheng", "声": "sheng", "升": "sheng", "胜": "sheng", "省": "sheng",
    "油": "you", "有": "you", "友": "you", "又": "you", "优": "you",
    "酱": "jiang", "江": "jiang", "姜": "jiang", "降": "jiang",
    "醋": "cu", "促": "cu", "粗": "cu",
    "大": "da", "打": "da", "达": "da",
    "米": "mi", "迷": "mi", "密": "mi", "蜜": "mi",
    "面": "mian", "棉": "mian", "免": "mian",
    "粉": "fen", "分": "fen", "份": "fen", "风": "fen",
    "方": "fang", "放": "fang", "房": "fang", "防": "fang",
    "便": "bian", "变": "bian", "遍": "bian", "边": "bian",
    "洗": "xi", "西": "xi", "吸": "xi", "喜": "xi",
    "发": "fa", "法": "fa", "罚": "fa",
    "衣": "yi", "一": "yi", "医": "yi", "依": "yi",
    "液": "ye", "也": "ye", "夜": "ye", "业": "ye",
    "纸": "zhi", "紫": "zhi", "子": "zhi", "只": "zhi", "直": "zhi", "汁": "zhi",
    "巾": "jin", "金": "jin", "斤": "jin", "进": "jin", "今": "jin", "紧": "jin",
    "冰": "bing", "饼": "bing", "并": "bing", "瓶": "bing",
    "干": "gan", "甘": "gan", "杆": "gan",
    "蛋": "dan", "淡": "dan", "但": "dan", "单": "dan",
    "鸡": "ji", "机": "ji", "基": "ji",
    "鸭": "ya", "牙": "ya", "压": "ya",
    "鱼": "yu", "余": "yu", "玉": "yu", "雨": "yu",
    "肉": "rou", "揉": "rou",
    "虾": "xia", "下": "xia", "夏": "xia",
    "蟹": "xie", "写": "xie", "谢": "xie",
    "瓜": "gua", "刮": "gua", "挂": "gua",
    "果": "guo", "国": "guo", "过": "guo", "锅": "guo",
    "苹": "ping", "平": "ping", "凭": "ping",
    "糖": "tang", "汤": "tang", "堂": "tang",
    "辣": "la", "蜡": "la", "拉": "la",
    "条": "tiao", "调": "tiao", "跳": "tiao",
    "豆": "dou", "都": "dou", "斗": "dou",
    "腐": "fu", "府": "fu",
    "绿": "lv", "虑": "lv", "旅": "lv",
    "乌": "wu", "无": "wu", "五": "wu", "午": "wu",
    "龙": "long", "隆": "long", "笼": "long",
    "柠": "ning", "宁": "ning", "凝": "ning",
    "檬": "meng", "梦": "meng", "猛": "meng",
    "百": "bai", "白": "bai", "摆": "bai",
    "事": "shi", "是": "shi", "世": "shi", "市": "shi",
    "芬": "fen", "纷": "fen",
    "达": "da",
    "加": "jia", "家": "jia", "佳": "jia", "嘉": "jia",
    "多": "duo", "朵": "duo", "躲": "duo",
    "宝": "bao", "包": "bao", "保": "bao", "饱": "bao",
    "怡": "yi", "宜": "yi", "以": "yi",
}

#: 统一去除规格空格/标点干扰
_STRIP_RE = re.compile(r"[\s\-_/·,.，。、()（）【】\[\]]+")


def _try_pypinyin(text: str) -> list[str] | None:
    """若环境装有 ``pypinyin`` 则使用，否则静默回落内置表。"""
    try:  # pragma: no cover - 可选依赖
        from pypinyin import lazy_pinyin

        return [_normalize_syllable(item) for item in lazy_pinyin(text)]
    except Exception:  # noqa: BLE001
        return None


def _normalize_syllable(syl: str) -> str:
    """把声母 n/l、zh/z、ch/c、sh/s、f/h 与韵母 in/ing、en/eng、an/ang 归一到宽容键。"""
    s = (syl or "").lower().strip()
    if not s:
        return ""
    for src, dst in (("zh", "z"), ("ch", "c"), ("sh", "s")):
        if s.startswith(src):
            s = dst + s[len(src):]
            break
    if s.startswith("l"):
        s = "n" + s[1:]
    if s.startswith("hu"):
        s = "f" + s[1:]
    for src, dst in (("ing", "in"), ("eng", "en"), ("ang", "an")):
        if s.endswith(src):
            s = s[: -len(src)] + dst
            break
    return s


def canonical_phonetic(text: str) -> str:
    """把文本转成方言归一的拼音/字符串。"""
    cleaned = _STRIP_RE.sub("", (text or "").strip().lower())
    if not cleaned:
        return ""
    tokens: list[str] = []
    py = _try_pypinyin(cleaned)
    for idx, ch in enumerate(cleaned):
        if ch in _CHAR_PHONETIC:
            tokens.append(_normalize_syllable(_CHAR_PHONETIC[ch]))
        elif py is not None and idx < len(py):
            tokens.append(py[idx])
        else:
            tokens.append(ch)
    return "|".join(tokens)


def fuzzy_score(query: str, target: str) -> float:
    """计算两个商品说法之间的综合相似度（0.0 ~ 1.0）。"""
    q_clean = _STRIP_RE.sub("", (query or "").strip().lower())
    t_clean = _STRIP_RE.sub("", (target or "").strip().lower())
    if not q_clean or not t_clean:
        return 0.0
    if q_clean == t_clean:
        return 1.0

    # 子串命中（如「可乐」 vs 「可乐 500ml」）给基础高分
    substr_score = 0.0
    if q_clean in t_clean or t_clean in q_clean:
        shorter = min(len(q_clean), len(t_clean))
        longer = max(len(q_clean), len(t_clean))
        substr_score = 0.72 + 0.23 * (shorter / longer)

    char_ratio = SequenceMatcher(None, q_clean, t_clean).ratio()
    q_phon = canonical_phonetic(q_clean)
    t_phon = canonical_phonetic(t_clean)
    phon_ratio = 0.0
    if q_phon and t_phon:
        if q_phon == t_phon:
            phon_ratio = 0.94
        else:
            phon_ratio = SequenceMatcher(None, q_phon.split("|"), t_phon.split("|")).ratio() * 0.92

    return round(max(substr_score, char_ratio * 0.90, phon_ratio), 4)


def find_similar(
    query: str,
    candidates: Iterable[tuple[T, Sequence[str]]],
    *,
    limit: int = 3,
    min_score: float = 0.60,
) -> list[tuple[T, float]]:
    """从候选中按最佳匹配词的相似度降序返回 Top N。"""
    scored: list[tuple[T, float]] = []
    for item, terms in candidates:
        best = 0.0
        for term in terms:
            score = fuzzy_score(query, term)
            if score > best:
                best = score
        if best >= min_score:
            scored.append((item, best))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[: max(1, limit)]
