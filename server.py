# 笔记管家 Web 后端：把 agent.py 的 CLI 循环暴露成 HTTP API
# 运行：python server.py  →  浏览器打开 http://127.0.0.1:8000
import io
import json
import os
import pathlib
import threading

import uvicorn
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent  # 复用你手写的 agent.py（工具/记忆/后台/定时全在里面）

app = FastAPI(title="笔记管家")

# ---- 会话状态：单一用户、单一会话，用锁防止并发聊天把 history 改乱 ----
_HISTORY_FILE = pathlib.Path(__file__).parent / ".state" / "history.json"


def _load_history():
    try:
        if _HISTORY_FILE.is_file():
            data = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _save_history():
    try:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HISTORY_FILE.write_text(json.dumps(_history, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


_history = _load_history()
_history_lock = threading.Lock()


class ChatIn(BaseModel):
    message: str


class AnalyzeIn(BaseModel):
    name: str
    content: str = ""   # 本地拖入时带内容；notes/ 内已有文件可为空


class SaveAnalysisIn(BaseModel):
    title: str = "分析摘要"
    analysis: dict = {}


class NameIn(BaseModel):
    name: str


class LocationIn(BaseModel):
    lat: float
    lon: float
    city: str = ""


class ImportIn(BaseModel):
    files: list = []   # [{name, content}] 拖入的 md 文件，落到 notes/载入/ 供管家 read_note 读取


@app.post("/api/location")
def save_location(body: LocationIn):
    """前端浏览器定位拿到经纬度后存下，供 fetch_weather 未指定城市时按你当前位置查询。"""
    try:
        agent._save_location(body.lat, body.lon, body.city)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/import")
def import_files(body: ImportIn):
    """把拖入的 md 文件落到 notes/载入/（防路径逃逸），返回落盘文件名。"""
    saved = []
    for f in body.files or []:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "").strip()
        content = str(f.get("content") or "")
        if not name or not content:
            continue
        safe = "载入/" + pathlib.Path(name).name          # 只取 basename，防止 ../ 逃逸
        path = agent.note_path(safe)
        if path is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        saved.append(safe)
    return {"ok": True, "saved": saved}


@app.post("/api/analyze")
def analyze(body: AnalyzeIn):
    """分析文件 → 返回结构化 analysis（前端渲染成友好卡片）；结构化失败才退回长文。"""
    text = body.content if body.content else agent.read_note(body.name)
    saved = False
    if body.content:
        path = agent.note_path(body.name)
        if path is None:
            return {"reply": "文件名不合法，只能保存到 notes/ 内"}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.content, encoding="utf-8")
        saved = True
    analysis = agent.analyze_structure(text) if (text and not str(text).startswith("Error")) else {}
    reply = ""
    if analysis:
        reply = analysis.get("summary") or "已生成分析。"
    else:
        with _history_lock:
            _history.append({"role": "user",
                             "content": f"请分析笔记文件 {body.name}：先读取它，再给出要点总结。"})
            reply = agent.agent_loop(_history)
        _save_history()
    return {"reply": reply, "saved": saved, "analysis": analysis, "name": body.name}


# ---- 文件文本提取：按扩展名把文档内容抽成纯文本，扫描件 PDF 自动 OCR ----
_ocr = None
_ocr_lock = threading.Lock()


def _ocr_pdf(data: bytes) -> str:
    """扫描件/图片型 PDF：渲染成图后用 OCR 识别中文。"""
    global _ocr
    import numpy as np
    import pymupdf
    with _ocr_lock:
        if _ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            _ocr = RapidOCR()
        ocr = _ocr
    doc = pymupdf.open(stream=data, filetype="pdf")
    pages = []
    for page in doc:
        pix = page.get_pixmap(dpi=200)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:
            img = img[:, :, :3]
        result, _ = ocr(img)
        if result:
            pages.append("\n".join(line[1] for line in result))
    doc.close()
    return "\n".join(pages).strip()


def _extract_text(filename: str, data: bytes):
    """按扩展名抽文本；不认识的返回 None。"""
    ext = pathlib.Path(filename).suffix.lower()
    if ext in (".txt", ".log", ".md", ".markdown", ".text"):
        for enc in ("utf-8", "gbk"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")
    if ext == ".docx":
        from docx import Document
        doc = Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    parts.append(" | ".join(cells))
        return "\n".join(parts)
    if ext == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        parts = []
        for ws in wb.worksheets:
            parts.append(f"## {ws.title}")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                if cells:
                    parts.append(" | ".join(cells))
        wb.close()
        return "\n".join(parts)
    if ext == ".pdf":
        import pymupdf
        doc = pymupdf.open(stream=data, filetype="pdf")
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        text = text.strip()
        if len(text) >= 30:          # 有足够文本层 → 直接取文字
            return text
        return _ocr_pdf(data)        # 文字太少 → 扫描件，走 OCR
    return None


def _extract_pdf_assets(data: bytes, assets_dir):
    """从 PDF 抠出结构图/流程图/配图，存到 notes/_assets/<笔记名>/ 供 md 引用。

    策略：内嵌位图优先（get_images），过滤掉过小的图标/logo/装饰；结构图/流程图多为
    矢量绘制、没有内嵌位图，仅当该页几乎无文字（纯图页）才整页渲染成 PNG 兜底。
    最后用 LLM 按各页文本做语义筛选，只留真正能梳理知识的图。返回 [(相对文件名, 页码)]。
    """
    import pymupdf
    assets_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(stream=data, filetype="pdf")
    saved = []
    seen_xrefs = set()
    page_texts = {}
    for pno, page in enumerate(doc, 1):
        page_texts[pno] = page.get_text()
        # 1) 内嵌位图（真截图/图片）
        imgs = page.get_images(full=True)
        for img in imgs:
            xref = img[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            try:
                pix = doc.extract_image(xref)
            except Exception:
                continue
            if not pix or not pix.get("image"):
                continue
            w = pix.get("width") or 0
            h = pix.get("height") or 0
            # 启发式过滤：过小的图基本都是图标/logo/装饰/表情，丢弃
            if w < 90 or h < 90:
                continue
            ext = (pix.get("ext") or "png").lower()
            if ext not in ("png", "jpg", "jpeg", "gif", "bmp", "webp"):
                ext = "png"
            fn = f"p{pno}_img{xref}.{ext}"
            (assets_dir / fn).write_bytes(pix["image"])
            saved.append({"file": fn, "page": pno})
        # 2) 矢量结构图/流程图：该页有绘图、没内嵌位图、且几乎无文字（纯图页）→ 整页渲染
        #    （含大量文字说明的页面整页渲染只会得到一张噪图，不保留）
        if not imgs and page.get_drawings() and len(page_texts[pno].strip()) < 80:
            pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
            fn = f"p{pno}_structure.png"
            (assets_dir / fn).write_bytes(pix.tobytes("png"))
            saved.append({"file": fn, "page": pno})
    doc.close()
    # LLM 语义筛选：只保留能梳理知识的结构化图/关键截图
    kept = agent.filter_assets(saved, page_texts)
    return kept if kept is not None else saved


async def _analyze_upload(file: UploadFile, only_pdf: bool):
    data = await file.read()
    name = file.filename or "未命名"
    if only_pdf and not name.lower().endswith(".pdf"):
        return {"reply": "这个接口只收 PDF 文件"}
    text = _extract_text(name, data)
    if text is None:
        return {"reply": f"暂不支持 {pathlib.Path(name).suffix} 格式（支持 .md/.txt/.log/.docx/.xlsx/.pdf）"}
    if not text.strip():
        return {"reply": "没提取到内容（可能是无法解析的图片型文档）"}
    note_name = pathlib.Path(name).stem + ".md"
    kind = pathlib.Path(name).suffix[1:].upper()
    # 拖入非 md 文件 = 想得到一篇完整笔记 → 走生产级两遍式（build_note），不再只出精简分析
    assets = []
    if pathlib.Path(name).suffix.lower() == ".pdf":
        assets = _extract_pdf_assets(data, agent.NOTES / "_assets" / pathlib.Path(name).stem)
    with _history_lock:
        notes = []
        res = ""
        for attempt in range(3):                # 结构化提取偶发失败 → 自动重试，保证"一定要完整笔记"
            agent.reset_note_events()
            res = agent.build_note(note_name, text, assets=assets)
            notes = agent.pop_note_events()
            if notes:
                break
            print(f"[build_note] 第{attempt + 1}次未产出笔记: {res}")
    if notes:
        return {"reply": res, "saved": note_name, "analysis": {}, "name": note_name, "notes": notes}
    # build_note 失败 → 退回快速分析，保证用户至少拿到可读内容
    with _history_lock:
        analysis = agent.analyze_structure(text)
        if analysis:
            # build_note 失败兜底：写结构化分析摘要，而不是把原文当成品笔记
            agent.write_note(note_name, agent.analysis_to_md(analysis))
        else:
            agent.write_note(note_name, text)      # 分析也失败才落盘原文留存
            _history.append({"role": "user",
                             "content": f"请分析 {kind} 文件提取出的内容（已存为 {note_name}）：先读取它，再给出要点总结。"})
            reply = agent.agent_loop(_history)
        _save_history()
    if analysis:
        reply = analysis.get("summary") or "已生成分析。"
    return {"reply": reply, "saved": note_name, "analysis": analysis, "name": note_name, "notes": []}


@app.post("/api/analyze-pdf")
async def analyze_pdf(file: UploadFile = File(...)):
    return await _analyze_upload(file, only_pdf=True)


@app.post("/api/analyze-file")
async def analyze_file(file: UploadFile = File(...)):
    return await _analyze_upload(file, only_pdf=False)


@app.post("/api/chat")
def chat(body: ChatIn):
    """把一句话丢给 agent_loop，返回管家的回复；同时返回本次生成的笔记产物。"""
    with _history_lock:
        agent.reset_note_events()
        _history.append({"role": "user", "content": body.message})
        reply = agent.agent_loop(_history)
        notes = agent.pop_note_events()
        _save_history()
    return {"reply": reply, "notes": notes}


@app.post("/api/save-analysis")
def save_analysis(body: SaveAnalysisIn):
    """把分析卡片保存成一篇规范笔记（notes/工作/ 下）。"""
    title = str(body.title or "分析摘要").strip().rstrip(".md") or "分析摘要"
    md = agent.analysis_to_md(body.analysis or {})
    if len(md) < 10:
        return {"ok": False, "error": "分析内容为空，无法保存"}
    name = f"工作/{title}.md"
    agent.write_note(name, md)
    return {"ok": True, "name": name}


@app.get("/api/notes")
def notes():
    return {"notes": agent.list_notes()}


@app.post("/api/delete-note")
def delete_note(body: NameIn):
    r = agent.delete_note(body.name)
    if r.startswith("Error"):
        return {"ok": False, "error": r}
    return {"ok": True, "message": r}


@app.get("/api/search")
def search(q: str = ""):
    return {"results": agent.search_notes(q)}

@app.get("/api/history")
def history_list(file: str = ""):
    return {"items": agent.list_history(file)}

@app.get("/api/history/content")
def history_content(file: str = "", snap: str = ""):
    c = agent.read_snapshot(file, snap)
    if c is None:
        return {"ok": False, "error": "快照不存在"}
    return {"ok": True, "content": c}

@app.post("/api/history/restore")
def history_restore(req: dict):
    r = agent.restore_snapshot(str(req.get("file") or ""), str(req.get("snap") or ""))
    return {"ok": not r.startswith("Error"), "message": r}

@app.delete("/api/history")
def history_delete(file: str = "", snap: str = ""):
    r = agent.delete_snapshot(file, snap)
    return {"ok": not r.startswith("Error"), "message": r}


@app.get("/api/notes/meta")
def notes_meta():
    """每篇笔记的结构化元数据（标题/类型/标签/状态），供仪表盘筛选。"""
    return {"notes": agent.list_note_meta()}


@app.get("/api/notes/{name}")
def read_note(name: str):
    return {"name": name, "content": agent.read_note(name)}


@app.get("/api/links/{name}")
def links(name: str):
    """双向链接：该笔记引用了谁(out)、谁引用了它(back)。"""
    return agent.note_links(name)


@app.post("/api/notes/{name}/open")
def open_note(name: str):
    """用系统默认程序打开笔记文件（Windows）。"""
    path = agent.note_path(name)
    if path is None or not path.is_file():
        return {"ok": False, "error": "文件不存在"}
    os.startfile(path)
    return {"ok": True}


@app.get("/api/notes/{name}/export-html")
def export_note_html(name: str):
    """导出单文件 HTML 用：返回 md，其中 _assets 图片替换成 data:URI base64 内嵌。
    前端拿这份 md 用 marked 渲染成 HTML 下载——单文件、不依赖 assets 图片目录，适合分享。"""
    md = agent.read_note(name)
    if not md or str(md).startswith("Error"):
        return {"ok": False, "error": "笔记不存在"}
    # 导出时剥掉 frontmatter 元信息（否则标题/类型/标签/来源/状态/日期会渲染成灰色文字）
    if md.startswith("---\n"):
        _end = md.find("\n---")
        if _end != -1:
            md = md[_end + 4:]
    import base64, re as _re
    assets_root = agent.NOTES / "_assets"

    def _embed(m):
        rel = m.group(1)
        fp = (assets_root / rel).resolve()
        if fp.is_file() and fp.is_relative_to(assets_root.resolve()):
            ext = fp.suffix.lower().lstrip(".") or "png"
            if ext == "jpg":
                ext = "jpeg"
            b64 = base64.b64encode(fp.read_bytes()).decode()
            return f"data:image/{ext};base64,{b64}"
        return m.group(0)

    body = _re.sub(r"_assets/([^)\s]+)", _embed, md)
    return {"ok": True, "name": name, "md": body}


@app.get("/api/todo")
def todo():
    return {
        "board": agent.todo.render(),
        "has_open": agent.todo.has_open_items(),
    }


@app.get("/api/schedules")
def schedules():
    items = agent.scheduler.items()
    return {"items": items, "count": len(items)}

@app.delete("/api/schedules/{sid}")
def schedules_del(sid: str):
    return {"ok": agent.scheduler.remove(sid)}

@app.post("/api/schedules")
async def schedules_add(req: dict):
    when = str(req.get("when") or "").strip()
    prompt = str(req.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "任务内容不能为空"}
    at_ts = agent._parse_at(when)
    if at_ts is None:
        return {"ok": False, "error": "时间无法解析，请用 19:50 或 2026-10-03 19:50"}
    sid = agent.scheduler.schedule_at(at_ts, prompt)
    return {"ok": True, "sid": sid}


@app.get("/api/bg")
def bg():
    return {"tasks": [
        {"id": t.task_id, "status": t.status,
         "result": t.result, "error": t.error}
        for t in agent.bg_tasks._tasks.values()
    ]}


@app.get("/api/tasks-log")
def tasks_log():
    """最近后台/定时任务执行记录（含 成功/产出/失败原因），供定时面板展示。"""
    return {"items": agent.list_task_logs()}


@app.post("/api/tasks/{tid}/rerun")
def task_rerun(tid: str):
    """按某条执行记录的 prompt 重新立即执行（失败重跑）。"""
    rec = next((r for r in agent.list_task_logs() if r.get("tid") == tid), None)
    if not rec:
        return {"ok": False, "error": "没有该任务的执行记录"}
    prompt = rec.get("prompt")
    if not prompt:
        return {"ok": False, "error": "该记录没有可重跑的内容"}
    new_tid = agent.bg_tasks.submit(prompt, sid=rec.get("sid"), kind=rec.get("kind") or "")
    return {"ok": True, "tid": new_tid}


@app.get("/api/memories")
def memories():
    items = []
    for f in agent.list_memories():
        rec = agent.read_memory(f)
        if rec:
            items.append(rec)
    return {"items": items}


class MemoryIn(BaseModel):
    name: str
    content: str


@app.put("/api/memories")
def save_memory(body: MemoryIn):
    name = body.name.replace(".md", "").strip()
    content = body.content.strip()
    if not name or not content:
        return {"ok": False, "error": "名字和内容不能为空"}
    agent.write_memory(name, content)
    return {"ok": True, "name": name}


@app.delete("/api/memories/{name}")
def delete_memory(name: str):
    agent.delete_memory(name)
    return {"ok": True}


# 服务启动即开调度线程：定时任务在后台照常触发
agent.scheduler.start()

app.mount("/static", StaticFiles(directory="static"), name="static")
(agent.NOTES / "_assets").mkdir(parents=True, exist_ok=True)   # 目录必须先存在，StaticFiles 才能挂载
app.mount("/_assets", StaticFiles(directory=agent.NOTES / "_assets"), name="assets")


@app.get("/")
def index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    print("笔记管家 Web · http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
