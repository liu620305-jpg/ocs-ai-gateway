"""题型归一化、提示词构造、答案解析。

这里的所有函数都是纯函数，方便单测。
"""

from __future__ import annotations

import json
import re

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# OCS 传来的 type 可能是多种写法，统一映射到内部三种题型
TYPE_ALIASES = {
    "single": "single",
    "single_choice": "single",
    "singlechoice": "single",
    "单选": "single",
    "单选题": "single",
    "multiple": "multiple",
    "multi": "multiple",
    "multiple_choice": "multiple",
    "多选": "multiple",
    "多选题": "multiple",
    "judge": "judge",
    "judgement": "judge",
    "judgment": "judge",
    "truefalse": "judge",
    "true_false": "judge",
    "tf": "judge",
    "判断": "judge",
    "判断题": "judge",
}

TYPE_LABELS = {"single": "单选题", "multiple": "多选题", "judge": "判断题"}


def normalize_type(raw: str | None) -> str | None:
    """把 OCS 传来的题型字符串归一化，无法识别返回 None。"""
    if not raw:
        return "single"
    key = str(raw).strip().lower().replace(" ", "").replace("-", "_")
    return TYPE_ALIASES.get(key)


def parse_options(raw) -> list[str]:
    """OCS 的 options 可能是 JSON 字符串、数组，或者带 A. 前缀的多行文本。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw]
    text = str(raw).strip()
    if not text:
        return []

    if text[0] in "[{":
        try:
            value = json.loads(text)
            if isinstance(value, list):
                return [str(x).strip() for x in value]
            if isinstance(value, dict):
                return [str(v).strip() for v in value.values()]
        except (json.JSONDecodeError, ValueError):
            pass

    lines = [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]
    if len(lines) > 1:
        # 去掉 "A. " / "A、" / "A)" 这类前缀，只保留选项正文
        return [re.sub(r"^[A-Za-z][\.\、\)\:：]\s*", "", line) for line in lines]
    return [text]


def build_messages(qtype: str, title: str, options: list[str]) -> list[dict]:
    """构造 Chat Completions 风格的 messages。"""
    system = (
        "你是一名严谨的答题助手。你必须只输出一行 JSON，"
        "不要输出解释、推理过程或 markdown 代码块。"
    )

    labeled = "\n".join(f"{LETTERS[i]}. {opt}" for i, opt in enumerate(options))

    if qtype == "single":
        body = (
            f"题型：单选题\n题干：{title}\n选项：\n{labeled}\n\n"
            '请选出唯一正确的选项，只输出 JSON，例如 {"answer": "A"}'
        )
    elif qtype == "multiple":
        body = (
            f"题型：多选题\n题干：{title}\n选项：\n{labeled}\n\n"
            "该题可能有多个正确答案，请给出全部正确选项的字母，"
            "按字母升序用 # 连接，只输出 JSON，例如 {\"answer\": \"A#C\"}"
        )
    else:
        body = (
            f"题型：判断题\n题干：{title}\n\n"
            '请判断该说法是否正确，只输出 JSON：{"answer": "正确"} 或 {"answer": "错误"}'
        )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": body},
    ]


# --------------------------------------------------------------------------
# 答案解析
# --------------------------------------------------------------------------

def _extract_answer_field(text: str) -> str:
    """从模型输出里尽量抠出答案本体。"""
    if not text:
        return ""
    raw = text.strip()

    # 1) 找 JSON 对象（模型偶尔会带前言或代码块）
    for match in re.finditer(r"\{[^{}]*\}", raw):
        try:
            obj = json.loads(match.group(0))
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            for key in ("answer", "答案", "result", "value", "choice"):
                if key in obj:
                    return str(obj[key]).strip()

    # 2) "答案：A" / "Answer: B" 这类
    match = re.search(r"(?:答案|作答|answer)\s*[:：]\s*([^\n\r]+)", raw, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    # 3) 整段当作答案
    return re.sub(r"```[a-zA-Z]*|```", "", raw).strip()


def _match_option_text(text: str, options: list[str]) -> str | None:
    """模型直接抄了选项原文时，反查它的字母。"""
    cleaned = re.sub(r"[\s。.，,；;：:\"'“”‘’]", "", text)
    if not cleaned:
        return None
    hits = [
        i
        for i, opt in enumerate(options)
        if re.sub(r"[\s。.，,；;：:\"'“”‘’]", "", opt) == cleaned
    ]
    if len(hits) == 1:
        return LETTERS[hits[0]]
    return None


def _parse_single(raw: str, options: list[str]) -> str | None:
    text = raw.strip().strip("\"'`。.，,；;")
    if not text:
        return None

    match = re.match(r"^[\(\（\[【]?\s*([A-Za-z])", text)
    candidates = [match.group(1)] if match else re.findall(r"[A-Za-z]", text)

    for letter in candidates:
        index = LETTERS.index(letter.upper())
        if index < len(options):
            return LETTERS[index]

    if options:
        by_text = _match_option_text(text, options)
        if by_text:
            return by_text
    return None


def _parse_multiple(raw: str, options: list[str]) -> str | None:
    text = raw.strip().strip("\"'`。.，,；;")
    letters = re.findall(r"[A-Za-z]", text)

    picked: list[int] = []
    for letter in letters:
        index = LETTERS.index(letter.upper())
        if index < len(options) and index not in picked:
            picked.append(index)

    if not picked and options:
        # 模型把选项原文用 # 或 、拼在一起
        parts = [p for p in re.split(r"[#、,，;；/|]+", text) if p.strip()]
        for part in parts:
            letter = _match_option_text(part, options)
            if letter:
                index = LETTERS.index(letter)
                if index not in picked:
                    picked.append(index)

    if not picked:
        return None
    return "#".join(LETTERS[i] for i in sorted(picked))


_TRUE_EXACT = {"1", "t", "true", "yes", "正确", "对", "是", "√", "✔", "对的", "正确的"}
_FALSE_EXACT = {"0", "f", "false", "no", "错误", "错", "否", "×", "✗", "✘", "不对", "错的", "不正确的"}


def _judge_value(text: str) -> str | None:
    cleaned = re.sub(r"[\s。.，,！!\"'“”‘’]", "", text).lower()
    if not cleaned:
        return None
    if cleaned in _TRUE_EXACT:
        return "正确"
    if cleaned in _FALSE_EXACT:
        return "错误"
    # 先判否定，避免 "不正确" 被当成 "正确"
    for token in ("不正确", "不对", "错误", "错", "否", "×", "✗", "✘", "false", "no"):
        if token in cleaned:
            return "错误"
    for token in ("正确", "对", "是", "√", "✔", "true", "yes"):
        if token in cleaned:
            return "正确"
    return None


def _parse_judge(raw: str, options: list[str]) -> str | None:
    text = raw.strip()
    if not text:
        return None

    # 判断题有时也返回 A/B，需要按选项内容落地
    match = re.match(r"^[\(\（\[【]?\s*([A-Za-z])\s*[\)\）\]】]?\s*$", text)
    if match and len(options) == 2:
        index = LETTERS.index(match.group(1).upper())
        if index < len(options):
            return _judge_value(options[index])
    return _judge_value(text)


def parse_answer(qtype: str, text: str, options: list[str]) -> str | None:
    """返回符合 OCS 规范的答案；解析失败返回 None（触发换下一个 AI）。"""
    raw = _extract_answer_field(text)
    if not raw:
        return None

    if qtype == "single":
        return _parse_single(raw, options)
    if qtype == "multiple":
        return _parse_multiple(raw, options)
    if qtype == "judge":
        return _parse_judge(raw, options)
    return None
