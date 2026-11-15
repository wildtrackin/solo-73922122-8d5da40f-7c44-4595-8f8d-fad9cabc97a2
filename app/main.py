"""临床数据匿名化发布 Web 应用（FastAPI）。"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import anonymizer as az
from . import database as db

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR.parent / "data" / "anonymizer.db"


def db_path() -> Path:
    return Path(os.environ.get("ANON_DB_PATH", str(DEFAULT_DB)))


def audit_historical_batches(path: Path) -> list[int]:
    """启动时核查所有已发布批次的实际可下载字节。

    病种含换行或文件无法解析的批次被持久标记为“待核查”并停止下载，
    原始字节留存供内部核对；已发布文件无法还原原始行边界，
    绝不将其误判为安全。返回新标记的批次号。
    """
    marked: list[int] = []
    with db.connect(path) as conn:
        for row in db.list_published(conn):
            reason = az.inspect_downloadable_csv(row["output_csv"])
            if reason is not None and db.mark_review(conn, row["id"], reason):
                marked.append(int(row["id"]))
    return marked


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db(db_path())
    marked = audit_historical_batches(db_path())
    if marked:
        print(f"[启动核查] {len(marked)} 个历史批次标记为待核查并停止下载："
              f"{marked}", file=sys.stderr)
    yield


app = FastAPI(title="临床数据匿名化发布平台", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _render(request: Request, template: str, status_code: int = 200, **ctx):
    return templates.TemplateResponse(
        request, template, ctx, status_code=status_code)


def _home_context() -> dict:
    with db.connect(db_path()) as conn:
        pending = db.get_latest_pending(conn)
        published = db.list_published(conn)
        review = db.list_review(conn)
    pending_plan = az.plan_from_json(pending["plan_json"]) if pending else None
    # 修复前生成的旧预览可能含跨行病种（引号吞并记录）：页面上明确拦截
    pending_blocked = bool(
        pending_plan
        and az.find_linebreak_rows(pending_plan.get("output_rows") or []))
    return {
        "pending": pending,
        "pending_plan": pending_plan,
        "pending_blocked": pending_blocked,
        "published": published,
        "review": review,
        "error": None,
        "info": None,
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return _render(request, "index.html", **_home_context())


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    raw = await file.read()
    filename = file.filename or "未命名.csv"
    try:
        rows = az.parse_csv(raw)
    except az.ValidationError as exc:
        ctx = _home_context()
        ctx["error"] = f"整批拒收：{exc}"
        return _render(request, "index.html", status_code=400, **ctx)

    with db.connect(db_path()) as conn:
        plan = az.find_best_plan(rows)
        if plan is None:
            batch_id = db.insert_rejected(conn, filename, len(rows))
            ctx = _home_context()
            ctx["error"] = (
                f"已接收批次 #{batch_id}（{len(rows)} 行），但在至多剔除 2 行、"
                "每组至少 3 人且含至少两种病种的约束下无可行匿名方案，不能发布。")
            return _render(request, "index.html", status_code=422, **ctx)

        batch_id = db.insert_pending(
            conn, filename, len(rows), az.plan_to_json(plan))

    return RedirectResponse(url=f"/?preview={batch_id}", status_code=303)


@app.post("/publish/{batch_id}")
def publish(request: Request, batch_id: int, confirm: str = Form("")):
    if confirm != "on":
        ctx = _home_context()
        ctx["error"] = "发布前必须勾选确认框"
        return _render(request, "index.html", status_code=400, **ctx)

    with db.connect(db_path()) as conn:
        try:
            row = db.get_batch(conn, batch_id)
            if row is None:
                raise LookupError("批次不存在")
            plan = az.plan_from_json(row["plan_json"])
            if plan is None:
                raise RuntimeError("匿名结果未通过发布前复核")
            output_rows = plan.get("output_rows") or []
            # 拦截修复前生成的旧待确认预览：跨行病种意味着引号吞并了
            # 受试者记录，发布会把被吞并的受试者号带进下载文件
            bad_rows = az.find_linebreak_rows(output_rows)
            if bad_rows:
                raise RuntimeError(
                    f"预览第 {'、'.join(map(str, bad_rows))} 行含跨行病种"
                    "（历史遗留的引号吞并记录），已拦截发布；"
                    "请重新上传原始 CSV 生成新预览")
            if not az.verify_output(output_rows):
                raise RuntimeError("匿名结果未通过发布前复核")
            output_csv = az.render_output_csv(output_rows)
            output_filename = f"anonymous_batch_{batch_id}.csv"
            db.publish_batch(conn, batch_id, output_csv, output_filename)
        except (LookupError, RuntimeError, ValueError) as exc:
            ctx = _home_context()
            ctx["error"] = str(exc)
            return _render(request, "index.html", status_code=409, **ctx)

    return RedirectResponse(url=f"/?published={batch_id}", status_code=303)


@app.get("/download/{batch_id}")
def download(batch_id: int):
    with db.connect(db_path()) as conn:
        # 状态判定与文件字节在同一查询快照中取得，保证顺序语义：
        # “待核查”标记先提交、之后读取字节的下载一律拦截；
        # 先取得字节（读取时标记尚未提交）的请求可以完成。
        row = db.get_batch(conn, batch_id)
        if row is None:
            return Response("批次不存在", status_code=404,
                            media_type="text/plain; charset=utf-8")
        if row["status"] == db.STATUS_REVIEW:
            return Response(
                "该批次已标记为待核查，下载已停止；原始字节留存供内部核对",
                status_code=403, media_type="text/plain; charset=utf-8")
        if row["status"] != db.STATUS_PUBLISHED:
            return Response("文件不存在或尚未发布", status_code=404,
                            media_type="text/plain; charset=utf-8")
        fname = row["output_filename"]
        return Response(
            content=row["output_csv"],
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition":
                    f"attachment; filename={fname}; filename*=UTF-8''{quote(fname)}"
            },
        )
