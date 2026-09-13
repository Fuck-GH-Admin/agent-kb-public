"""多模态内容抽取：文本/代码、图片、音视频、PDF、Office 文档、Jupyter、模型权重；
未知格式只做元数据（隔离不污染索引）。"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import struct
import subprocess
import zipfile

from .util import guess_kind, human_size, read_text, sniff_kind

_XML_TAG_RE = re.compile(r"<[^>]+>")


def _inspect_text(path: str, cfg) -> dict:
    cap = int(cfg.get("ingest.max_text_kb") or 256) * 1024
    text = read_text(path, max_bytes=cap)
    truncated = os.path.getsize(path) > cap
    return {"text": text, "meta": {"truncated": truncated}}


def _inspect_office(path: str, cfg) -> dict:
    """Office/ODF（zip 容器）：抽 XML 正文并剥标签。零依赖，够编目用。"""
    parts = {"word/document.xml", "content.xml", "xl/sharedStrings.xml",
             "ppt/slides/slide1.xml", "ppt/slides/slide2.xml"}
    texts, meta = [], {}
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            meta["container"] = "office-openxml" if "word/document.xml" in names \
                or "xl/sharedStrings.xml" in names else "odf"
            for name in parts & names:
                raw = z.read(name)[:512 * 1024].decode("utf-8", errors="replace")
                stripped = _XML_TAG_RE.sub(" ", raw)
                stripped = " ".join(stripped.split())
                if stripped:
                    texts.append(stripped)
    except (zipfile.BadZipFile, OSError, KeyError) as e:
        meta["error"] = f"office 解析失败: {e}"
    text = "\n".join(texts) or None
    cap = int(cfg.get("ingest.max_text_kb") or 256) * 1024
    return {"text": (text or "")[:cap] or None, "meta": meta}


def _inspect_ipynb(path: str, cfg) -> dict:
    """Jupyter：抽 markdown+code 单元为可读文本，不带 base64 输出（防索引污染）。"""
    meta = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            nb = json.load(f)
        cells = nb.get("cells", [])
        meta["cells"] = len(cells)
        parts = []
        for c in cells[:200]:
            src = "".join(c.get("source") or [])
            if not src.strip():
                continue
            tag = "# %%" if c.get("cell_type") == "code" else ""
            parts.append(f"{tag}\n{src}" if tag else src)
        text = "\n\n".join(parts) or None
    except (json.JSONDecodeError, OSError) as e:
        meta["error"] = f"ipynb 解析失败: {e}"
        text = None
    cap = int(cfg.get("ingest.max_text_kb") or 256) * 1024
    return {"text": (text or "")[:cap] or None, "meta": meta}


def _inspect_model(path: str) -> dict:
    """模型权重：只读头部元数据（张量数/dtype/架构线索），绝不加载权重本体。"""
    ext = os.path.splitext(path)[1].lower()
    meta: dict = {"format": ext.lstrip(".")}
    try:
        if ext == ".safetensors":
            with open(path, "rb") as f:
                (hlen,) = struct.unpack("<Q", f.read(8))
                hdr = json.loads(f.read(min(hlen, 4 * 1024 * 1024)))
            tensors = {k: v for k, v in hdr.items() if k != "__metadata__"}
            meta["tensors"] = len(tensors)
            meta["dtypes"] = sorted({v.get("dtype", "?") for v in tensors.values()})[:4]
            extra = hdr.get("__metadata__") or {}
            for k in ("format", "architecture", "model_type", "name"):
                if extra.get(k):
                    meta[k] = str(extra[k])[:80]
        elif ext == ".gguf":
            with open(path, "rb") as f:
                magic = f.read(4)
                if magic == b"GGUF":
                    (version,) = struct.unpack("<I", f.read(4))
                    meta["gguf_version"] = version
        elif ext in (".pt", ".pth", ".ckpt"):
            with open(path, "rb") as f:
                head = f.read(4)
            meta["container"] = "zip" if head[:2] == b"PK" else "pickle-legacy"
            if meta["container"] == "pickle-legacy":
                meta["warning"] = "legacy pickle 格式，反序列化可执行任意代码，仅存储勿加载"
    except (OSError, struct.error, json.JSONDecodeError, ValueError) as e:
        meta["error"] = f"模型头解析失败: {e}"
    return {"text": None, "meta": meta}


def _inspect_pdf(path: str, cfg) -> dict:
    raw_cap = cfg.get("ingest.pdf_max_pages")
    pages_cap = int(raw_cap) if raw_cap is not None else 8
    if pages_cap <= 0:
        pages_cap = None  # 0/负数 = 不限页
    text, meta = None, {}
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        meta["pages"] = len(reader.pages)
        if pages_cap is not None and meta["pages"] > pages_cap:
            meta["truncated_pages"] = pages_cap
        text = "\n".join(
            (p.extract_text() or "") for p in reader.pages[:pages_cap])
        text = text or None
    except Exception:
        if shutil.which("pdftotext"):
            try:
                cmd = ["pdftotext"] + (["-l", str(pages_cap)] if pages_cap else []) \
                    + [path, "-"]
                out = subprocess.run(cmd, capture_output=True, timeout=60,
                                     check=False)
                if out.returncode == 0:
                    text = out.stdout.decode("utf-8", errors="replace") or None
            except (subprocess.TimeoutExpired, OSError):
                pass
    return {"text": text, "meta": meta}


def _inspect_image(path: str) -> dict:
    meta = {}
    try:
        from PIL import Image
        with Image.open(path) as im:
            meta["width"], meta["height"] = im.size
            meta["format"] = im.format or ""
            exif = im.getexif()
            for tag, key in ((306, "taken_at"), (271, "camera_make"), (272, "camera_model")):
                val = exif.get(tag)
                if val:
                    meta[key] = str(val)
    except Exception as e:
        meta["error"] = f"图片解析失败: {e}"
    return {"text": None, "meta": meta}


def _inspect_av(path: str) -> dict:
    meta = {}
    if shutil.which("ffprobe"):
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json",
                 "-show_format", "-show_streams", path],
                capture_output=True, timeout=60, check=False)
            if out.returncode == 0:
                info = json.loads(out.stdout.decode() or "{}")
                fmt = info.get("format", {})
                if fmt.get("duration"):
                    meta["duration"] = float(fmt["duration"])
                streams = info.get("streams", [])
                for s in streams:
                    if s.get("codec_type") == "video" and s.get("width"):
                        meta["width"], meta["height"] = s["width"], s["height"]
                        meta["codec"] = s.get("codec_name", "")
                        break
                else:
                    for s in streams:
                        if s.get("codec_type") == "audio":
                            meta["codec"] = s.get("codec_name", "")
                            break
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass
    return {"text": None, "meta": meta}


_SIG_RE = re.compile(
    r"^\s*(?:(?:async\s+)?def\s+\w+|class\s+\w+|(?:export\s+)?(?:async\s+)?function\s+\w+"
    r"|(?:pub\s+)?fn\s+\w+|func\s+\w+|type\s+\w+|interface\s+\w+"
    r"|(?:public|private|protected)[\w\s<>\[\]]*\s+\w+\s*\()", re.M)


def code_outline(text: str, limit: int = 12) -> str:
    """代码结构感知摘要：抽函数/类签名，胜过"正文前几行"（DESIGN-V2 C8）。"""
    sigs = []
    for line in text.splitlines():
        if _SIG_RE.match(line):
            s = line.strip().rstrip(":{").strip()
            if s and s not in sigs:
                sigs.append(s[:120])
        if len(sigs) >= limit:
            break
    return "；".join(sigs)


def inspect_file(path: str, cfg) -> dict:
    """返回 {kind, mime, ext, size, meta, text}。text 为可索引文本或 None。"""
    kind = sniff_kind(path, guess_kind(path))
    mime = mimetypes.guess_type(path)[0] or ""
    ext = os.path.splitext(path)[1].lower()
    size = os.path.getsize(path)
    meta: dict = {}
    text = None
    if kind == "model":
        r = _inspect_model(path)
        meta = r["meta"]
    elif ext in (".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"):
        r = _inspect_office(path, cfg)
        text, meta = r["text"], r["meta"]
    elif ext == ".ipynb":
        r = _inspect_ipynb(path, cfg)
        text, meta = r["text"], r["meta"]
    elif kind in ("text", "code"):
        r = _inspect_text(path, cfg)
        text, meta = r["text"], r["meta"]
    elif kind == "pdf":
        r = _inspect_pdf(path, cfg)
        text, meta = r["text"], r["meta"]
    elif kind == "image":
        r = _inspect_image(path)
        meta = r["meta"]
    elif kind in ("audio", "video"):
        r = _inspect_av(path)
        meta = r["meta"]
    return {"kind": kind, "mime": mime, "ext": ext, "size": size,
            "meta": meta, "text": text}


def make_title(path: str, kind: str, text: str | None) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    if text:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):  # markdown 标题优先
                return line.lstrip("#").strip()[:100] or stem
            break
    return stem[:100]
