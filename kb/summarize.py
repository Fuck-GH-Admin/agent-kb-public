"""摘要与关键词：默认离线抽取式；可配置 OpenAI 兼容 LLM 生成（失败自动回退）。

支持模型故障转移（summarize.models，逗号分隔按序尝试），适配"中转后端多为
非指令跟随 chat 模型"的现实：system 提示会被忽略时，回退为把编目指令直接
写进 user 消息，并优先解析 ```json 代码块。
"""
from __future__ import annotations

import json
import re
import urllib.request

from .util import extractive_summary, human_size, keywords


class LLM:
    def __init__(self, api_base: str, api_key: str, model: str):
        if not api_base or not model:
            raise RuntimeError("summarize.provider=api 需要 summarize.api_base 与 summarize.model")
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        # 主模型 + 备选（summarize.models），失败按序转移
        self.models = [m.strip() for m in (model or "").split(",") if m.strip()]
        self.model = self.models[0]

    def _post(self, body: dict, timeout: int = 90) -> dict:
        req = urllib.request.Request(
            self.api_base + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def _ask(self, system: str, user: str, force_json: bool) -> str:
        """单模型单请求。返回 content 文本；失败抛异常（由调用方转移/回退）。"""
        body = {"model": self.model, "temperature": 0.2, "max_tokens": 1000,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        if force_json:
            body["response_format"] = {"type": "json_object"}
        data = self._post(body)
        text = (data["choices"][0]["message"].get("content") or "").strip()
        if not text:
            raise RuntimeError("empty completion")
        return text

    def chat_json(self, system: str, user: str, max_chars: int) -> dict | None:
        """按模型优先级依次请求；任一模型解析成功即返回。全部失败 → None（上层回退抽取式）。

        正文截断上限 60000 字符：编目要看到文件后半部分的定义（真实教训：
        core.py 的 drill 在 18.9k 处，12k 截断导致 AI 摘要漏掉该功能）。
        """
        user_full = (f"{system}\n\n【待编目内容】\n{user[:60000]}\n\n"
                     "只输出一个 JSON 对象，字段固定为 title/summary/keywords，"
                     "不要输出其他任何文字或解释。")
        for model in self.models:
            self.model = model
            for attempt in ("json_mode", "plain"):
                try:
                    text = self._ask(system, user_full, force_json=(attempt == "json_mode"))
                except Exception:
                    continue  # 模型级/请求级失败 → 换策略，再不行换模型
                out = _parse_json_obj(text)
                if out:
                    title = str(out.get("title") or "")[:100]
                    summary = str(out.get("summary") or "")[:max_chars]
                    kws = out.get("keywords")
                    if isinstance(kws, list):
                        kws = ",".join(str(k) for k in kws[:10])
                    return {"title": title, "summary": summary,
                            "keywords": str(kws or "")[:200]}
        return None


def _parse_json_obj(text: str) -> dict | None:
    """从模型输出提取 JSON 对象：```json 块优先，其次首个平衡 {}。"""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = []
    if m:
        candidates.append(m.group(1))
    m2 = re.search(r"\{.*\}", text, re.S)
    if m2:
        candidates.append(m2.group(0))
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


_SUM_SYS = ("你是知识库编目员。为文档生成中文编目信息，只输出 JSON："
            '{"title": "...", "summary": "不超过200字的摘要", "keywords": ["关键词", ...]}。'
            "title 简洁准确；summary 概括核心内容；keywords 5-10个。")


def build_summary(kind: str, fallback_title: str, text: str | None, meta: dict,
                  size: int, cfg, llm: LLM | None) -> tuple[str, str, str]:
    """返回 (title, summary, keywords)。LLM 优先，失败回退抽取式/元数据描述。

    ingest.summarize_on_ingest=offline（默认）时忽略 llm——批量摄取走离线摘要，
    AI 编目集中交给 `kb curate --ai`（显式、可重跑、可限速）。逐文件 LLM 摘要
    ~2.5s/文件，40 万文件的库等于不可行（真实教训）。
    """
    max_chars = int(cfg.get("summarize.max_chars") or 400)
    use_llm = llm if (cfg.get("ingest.summarize_on_ingest") == "ai") else None
    if text and text.strip():
        if use_llm is not None:
            out = use_llm.chat_json(_SUM_SYS, f"文件名: {fallback_title}\n\n{text}", max_chars)
            if out and out.get("summary"):
                return out["title"] or fallback_title, out["summary"], out["keywords"] or \
                    keywords(text)
        if kind == "code":  # 代码按结构摘要（函数/类签名）而非正文前几行
            from .extract import code_outline
            outline = code_outline(text)
            if outline:
                return fallback_title, outline[:max_chars], keywords(text)
        summ = extractive_summary(text, max_chars=max_chars)
        return fallback_title, summ, keywords(text)
    # 无文本内容：用元数据描述
    desc = [fallback_title]
    if kind == "image":
        w, h = meta.get("width"), meta.get("height")
        if w:
            desc.append(f"图片 {w}x{h} {meta.get('format', '')}".strip())
    elif kind in ("audio", "video"):
        dur = meta.get("duration")
        d = f"{float(dur):.0f}秒" if dur else "未知时长"
        desc.append(f"{kind}文件 {d} 编码{meta.get('codec', '未知')}")
    elif kind == "model":
        bits = [f"{meta.get('format', '模型')}权重 {human_size(size)}"]
        if meta.get("tensors"):
            bits.append(f"{meta['tensors']}个张量 {'/'.join(meta.get('dtypes', []))}")
        for k in ("architecture", "model_type", "name"):
            if meta.get(k):
                bits.append(str(meta[k]))
        if meta.get("warning"):
            bits.append(meta["warning"])
        desc.append(" ".join(bits))
    elif kind == "pdf":
        p = meta.get("pages")
        desc.append(f"PDF 文档{f' {p}页' if p else ''}（未安装 pypdf/pdftotext，未抽取文本）")
    else:
        desc.append(f"{kind}文件 {human_size(size)}（未识别出可索引文本）")
    kw = keywords(fallback_title)
    return fallback_title, "；".join(desc[1:]) or desc[0], kw
