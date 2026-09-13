"""通用工具：哈希、CJK 感知分词、关键词、抽取式摘要、写锁、日志轮转、内容嗅探。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from collections import Counter
from datetime import datetime, timezone

try:  # 可选依赖：装了 jieba 中文关键词质量更好，没装用二元组保底
    import jieba as _jieba
    _jieba.setLogLevel(60)
except ImportError:
    _jieba = None

_CJK_RE = re.compile(
    "[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)
_WORD_RE = re.compile(r"[a-z0-9]+")
_SENT_RE = re.compile(r"[^.!?\n。！？；;]+[.!?\n。！？；;]?")

TEXT_EXTS = {".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
             ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".html", ".htm",
             ".xml", ".srt", ".ass", ".tex", ".bib", ".properties"}
CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".c", ".h", ".cpp", ".hpp", ".cc",
             ".java", ".kt", ".go", ".rs", ".rb", ".php", ".sh", ".bash", ".zsh",
             ".ps1", ".bat", ".sql", ".r", ".lua", ".pl", ".swift", ".scala",
             ".vim", ".cmake", ".make", ".mk", ".proto", ".graphql", ".vue",
             ".css", ".scss", ".less", ".ipynb"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif",
              ".svg", ".ico", ".heic", ".avif"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".ts"}
PDF_EXTS = {".pdf"}
ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".zst"}
# 扩展名在多种类型间冲突，内容为文本时应判为代码（.ts: TypeScript vs MPEG-TS）
AMBIGUOUS_CODE_EXTS = {".ts", ".m", ".rs", ".pl", ".cls"}
# 模型权重：zip/自定义容器居多，扩展名优先于 magic 嗅探
MODEL_EXTS = {".safetensors", ".gguf", ".pt", ".pth", ".onnx", ".ckpt", ".tflite"}
# Office/ODF 文档：zip 容器但本质是文档，抽取正文而非判 archive
OFFICE_EXTS = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}

_EN_STOP = {"the", "and", "of", "to", "in", "is", "are", "was", "for", "on", "with",
            "a", "an", "this", "that", "it", "as", "be", "by", "or", "from", "at",
            "not", "we", "you", "your", "can", "will", "if", "then", "than", "so",
            "there", "here", "what", "which", "into", "about", "their", "they",
            "have", "has", "had", "but", "its", "our", "these", "those", "when"}
_ZH_STOP_CHARS = set("的了是在和与及对中为等我这那也就都不有个人大小上下之一吗呢吧啊很要")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def blob_rel(digest: str) -> str:
    return os.path.join(digest[:2], digest[2:4], digest)


def guess_kind(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in MODEL_EXTS:
        return "model"
    if ext in OFFICE_EXTS:
        return "text"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in TEXT_EXTS or ext in CODE_EXTS:
        return "text" if ext in TEXT_EXTS else "code"
    return "binary"


def read_text(path: str, max_bytes: int = 262144) -> str:
    """按字节上限读文本，多编码尝试。

    关键：按字节截断可能把多字节字符切半——必须用增量解码器且 final=False
    容忍被截断的尾部，否则 GBK 大文件会被误判"不是 gb18030"而回退成整篇
    乱码（真实发生过：围城.txt 4096 字节预览里找不到"方鸿渐"）。
    """
    import codecs
    with open(path, "rb") as f:
        raw = f.read(max_bytes)
    truncated = len(raw) == max_bytes
    for enc in ("utf-8", "gb18030"):
        try:
            dec = codecs.getincrementaldecoder(enc)()
            return dec.decode(raw, final=not truncated)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def is_cjk(ch: str) -> bool:
    return bool(_CJK_RE.match(ch))


def fts_index_text(s: str) -> str:
    """索引前预处理：CJK 逐字切分、拉丁词保留、其余折叠为空格。"""
    out = []
    for ch in s.lower():
        if is_cjk(ch):
            out.append(" " + ch + " ")
        elif ch.isalnum():
            out.append(ch)
        else:
            out.append(" ")
    return " ".join("".join(out).split())


def _cjk_run_alts(chars: list[str]) -> list[str] | None:
    """长 CJK 串的拆词策略：jieba 分词优先，回退重叠二元组。"""
    s = "".join(chars)
    if _jieba is not None:
        words = [w.strip() for w in _jieba.cut(s) if len(w.strip()) >= 2]
        if len(words) >= 2:
            return words
    return [s[i:i + 2] for i in range(len(s) - 1)]


def _sp(term: str) -> str:
    """拆词项转 MATCH 引号形式：词/二元组内部逐字空格分隔。"""
    return " ".join(term)


def fts_query(q: str) -> str:
    """构造 FTS5 MATCH 表达式。

    - ≤3 字的 CJK 段：整串相邻短语。
    - >3 字的 CJK 段：jieba 分词（可选）或重叠二元组，取"相邻项 AND、组间
      OR"——文档命中查询串的连续 3 字子串即可召回（真实教训："面对行刑队"
      查不到"站在行刑队"；纯 OR 又会因稀有二元组高 IDF 放进噪声）。
    - 段界 = 拉丁字符或结尾；查询里的空格/标点也会切段（"多年以后 面对行刑队"
      → 4 字段 AND 5 字段，两段各自整短语）。
    - 拉丁词：前缀匹配（≥3 字符加 *）。
    """
    parts: list[str] = []
    word: list[str] = []
    run: list[str] = []

    def flush_word():
        if word:
            w = "".join(word)
            parts.append(f'"{w} *"' if len(w) >= 3 else f'"{w}"')
            word.clear()

    def flush_run():
        if not run:
            return
        if len(run) <= 3:
            # ≤3 字整短语（"红烧肉"）；4 字以上走拆词路——4 字意译（如
            # "注意力机制" vs "自注意力机制"）同样需要子串召回
            parts.append('"' + " ".join(run) + '"')
        else:
            terms = _cjk_run_alts(run)[:24]
            if len(terms) == 1:
                parts.append('"' + " ".join(terms[0]) + '"')
            else:
                disjuncts = []
                for a, b in zip(terms, terms[1:]):
                    disjuncts.append(f'("{_sp(a)}" AND "{_sp(b)}")')
                parts.append("(" + " OR ".join(disjuncts) + ")")
        run.clear()

    for ch in q.lower():
        if is_cjk(ch):
            flush_word()
            run.append(ch)     # CJK 之间无论原查询有无分隔符都属同一段
        elif ch.isalnum():
            flush_run()
            word.append(ch)
        else:
            flush_word()
            flush_run()        # 分隔符总是段界；跨段召回由"组间 OR"承担
    flush_word()
    flush_run()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    # 混合语言查询（如"灾备演练 drill"）：跨语言 AND 过严——中文摘要与英文术语
    # 很少同时出现（真实教训：core.py 摘要有"灾备演练"没有"drill"，整查询 0 命中）。
    # 策略：同级词项取 OR，召回优先；两路（关键词路+向量路）融合会再压噪声。
    return "(" + " OR ".join(parts) + ")"


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_RE.findall(text) if s.strip()]


def token_freq(text: str) -> Counter:
    """词频统计：拉丁词（len>=2）+ CJK 二元组，过滤停用成分。"""
    freq: Counter = Counter()
    low = text.lower()
    for w in _WORD_RE.findall(low):
        if len(w) >= 2 and w not in _EN_STOP and not w.isdigit():
            freq[w] += 1
    run = []
    for ch in low:
        if is_cjk(ch):
            run.append(ch)
        else:
            run.clear()
            continue
        if len(run) == 2:
            if run[0] not in _ZH_STOP_CHARS and run[1] not in _ZH_STOP_CHARS:
                freq[run[0] + run[1]] += 1
            run.pop(0)
    return freq


def keywords(text: str, n: int = 8) -> str:
    """关键词：装了 jieba 用词级切分（质量好），否则回退二元组（保底不难看）。"""
    if not text:
        return ""
    if _jieba is not None:
        freq: Counter = Counter()
        for w in _jieba.cut(text[:20000]):
            w = w.strip().lower()
            if len(w) < 2 or w in _EN_STOP or w.isdigit():
                continue
            if all(ch in _ZH_STOP_CHARS for ch in w):
                continue
            if not any(ch.isalnum() or is_cjk(ch) for ch in w):
                continue
            freq[w] += 1
        if freq:
            return ",".join(w for w, _ in freq.most_common(n))
    freq = token_freq(text)
    return ",".join(w for w, _ in freq.most_common(n))


def make_snippet(text: str, query: str, width: int = 120) -> str | None:
    """从原文（非 FTS 索引列）截命中片段，避免 CJK 逐字切分导致的空格观感。"""
    if not text or not query:
        return None
    low = text.lower()
    terms = [t for t in re.split(r"[\s,，。;；、]+", query.lower()) if t]
    # CJK 连串再拆一层，提高定位命中率
    for t in list(terms):
        if len(t) > 4 and any(is_cjk(c) for c in t):
            terms.append(t[:2])
    pos = -1
    for t in terms:
        pos = low.find(t)
        if pos >= 0:
            break
    if pos < 0:
        return None
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    frag = text[start:end].replace("\n", " ").strip()
    return ("…" if start > 0 else "") + frag + ("…" if end < len(text) else "")


_MAGIC = (
    (b"%PDF-", "pdf"), (b"\x89PNG\r\n\x1a\n", "image"), (b"\xff\xd8\xff", "image"),
    (b"GIF87a", "image"), (b"GIF89a", "image"), (b"BM", "image"),
    (b"ID3", "audio"), (b"OggS", "audio"), (b"fLaC", "audio"),
    (b"\x1aE\xdf\xa3", "video"), (b"PK\x03\x04", "archive"),
    (b"\x1f\x8b", "archive"), (b"7z\xbc\xaf\x27\x1c", "archive"),
)


def sniff_kind(path: str, ext_kind: str) -> str:
    """内容嗅探优先于扩展名：修 .ts(TypeScript) 被判成 MPEG-TS 视频这类冲突。

    例外：模型权重与 Office 文档多为 zip/自定义容器，magic 会误判 archive，
    这两类以扩展名为准（guess_kind 已定型），不进嗅探。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext_kind == "model" or ext in OFFICE_EXTS:
        return ext_kind
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return ext_kind
    if not head:
        return ext_kind
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    if head[4:12] in (b"ftypisom", b"ftypmp42", b"ftypM4V ", b"ftypqt  "):
        return "video"
    if head[:4] == b"RIFF":
        return "audio" if head[8:12] == b"WAVE" else "video"
    if b"\x00" in head:          # 有 NUL 基本可判定为二进制
        return ext_kind if ext_kind in ("image", "audio", "video", "pdf") else "binary"
    try:                          # 可解码为文本 → 按扩展名在 text/code 里选
        head.decode("utf-8")
    except UnicodeDecodeError:
        try:
            head.decode("gb18030")
        except UnicodeDecodeError:
            return ext_kind
    if ext_kind in ("text", "code"):
        return ext_kind
    # 扩展名判成二进制类但内容是文本：歧义扩展名按代码处理（.ts 既是
    # TypeScript 也是 MPEG-TS），其余按纯文本
    ext = os.path.splitext(path)[1].lower()
    return "code" if ext in AMBIGUOUS_CODE_EXTS else "text"


class WriteLock:
    """KB_HOME 级写者互斥（flock，非阻塞）。防止并发写造成向量元数据竞态。

    可重入：同一实例嵌套获取只在最外层真正加/解锁（reject→remove 这类
    嵌套写路径必须如此，否则第二次 open 的 fd 会与自己冲突而死锁）。
    Windows 无 fcntl，降级为无锁（单机单写者仍安全），doctor 会提示。
    """

    def __init__(self, home: str):
        self.path = os.path.join(home, ".write.lock")
        self._fh = None
        self._depth = 0

    def __enter__(self):
        self._depth += 1
        if self._depth > 1:
            return self
        try:
            import fcntl
        except ImportError:
            return self
        self._fh = open(self.path, "w")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            self._depth -= 1
            raise RuntimeError(
                "另一个 kb 写进程正在运行（写操作互斥）。请等待其完成后重试")
        return self

    def __exit__(self, *exc):
        self._depth = max(0, self._depth - 1)
        if self._depth > 0:
            return False
        if self._fh is not None:
            try:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            self._fh.close()
            self._fh = None
        return False


def append_jsonl(path: str, rec: dict, max_mb: int = 32, keep: int = 3) -> None:
    """带轮转的 JSONL 追加：超过 max_mb 滚为 .1/.2…，最多留 keep 份。"""
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_mb * 1024 * 1024:
            for i in range(keep - 1, 0, -1):
                older, newer = f"{path}.{i + 1}", f"{path}.{i}"
                if os.path.exists(newer):
                    os.replace(newer, older)
            os.replace(path, f"{path}.1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 日志失败绝不影响主流程


def post_webhook(url: str, payload: dict, timeout: int = 10) -> bool:
    """告警钩子：失败通知。返回是否成功，不抛异常。"""
    if not url:
        return False
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def extractive_summary(text: str, max_chars: int = 400, max_sentences: int = 4) -> str:
    text = text.strip()
    if not text:
        return ""
    sentences = split_sentences(text)
    if not sentences:
        return text[:max_chars]
    freq = token_freq(text)
    scored = []
    for i, s in enumerate(sentences):
        toks = _WORD_RE.findall(s.lower()) + [
            s[j:j + 2] for j in range(len(s) - 1) if is_cjk(s[j]) and is_cjk(s[j + 1])
        ]
        if not toks:
            continue
        score = sum(freq.get(t, 0) for t in toks) / (len(toks) ** 0.5 + 1)
        scored.append((score, i))
    scored.sort(reverse=True)
    chosen = sorted(i for _, i in scored[:max_sentences])
    out = ""
    for i in chosen:
        if len(out) + len(sentences[i]) > max_chars and out:
            break
        out += sentences[i]
    return (out or text[:max_chars])[:max_chars * 2]


def entry_text(kb, row) -> str | None:
    """取条目正文：in_place 读源文件，否则读 blob；都没有回退 preview。"""
    import os
    src = row.get("source_path")
    if row.get("in_place") and src and os.path.exists(src):
        return read_text(src)
    if row.get("blob") and kb.blobs.exists(row["blob"]):
        return read_text(kb.blobs.path(row["blob"]))
    return row.get("preview") or None


def chunk_text(text: str, size: int = 1200, overlap: int = 200,
               max_chunks: int = 2000) -> list[str]:
    """确定性离线分块：段落优先合并，超长段内滑窗+重叠。
    返回块文本列表（不含偏移，偏移由摄取层记录）。"""
    if size <= overlap:
        overlap = max(0, size // 5)
    chunks: list[str] = []

    def slide(long: str):
        step = size - overlap
        i = 0
        while i < len(long):
            if len(chunks) >= max_chunks:
                return
            chunks.append(long[i:i + size])
            if i + size >= len(long):
                break
            i += step

    buf = ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(chunks) >= max_chunks:
            break
        if len(para) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            slide(para)
        elif len(buf) + len(para) + 1 <= size:
            buf = f"{buf}\n{para}" if buf else para
        else:
            chunks.append(buf)
            buf = para
    if buf and len(chunks) < max_chunks:
        chunks.append(buf)
    return chunks


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}PB"
