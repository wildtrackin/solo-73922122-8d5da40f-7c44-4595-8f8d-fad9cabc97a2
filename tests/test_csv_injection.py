"""引号吞并记录漏洞的回归测试：

* 上传校验：引号不完整 / 病种跨行 -> 整批拒收；
* 发布拦截：修复前生成的旧待确认预览含跨行病种 -> 发布 409；
* 启动核查：历史已发布批次实际字节异常（病种含换行 / 无法解析）
  -> 持久标记“待核查”、停止下载、原字节留存；
* 下载顺序：标记先提交后读字节 -> 拦截；先取得字节 -> 可完成；
* 单行病种文件与人工确认流程保持不变。
"""
import csv
import importlib
import io
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

from app import anonymizer as az
from app import database as db
from app.anonymizer import AGE, DISEASE, ZIP, ValidationError


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("ANON_DB_PATH", str(db_file))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c, db_file


def _csv_bytes(rows, header="受试者号,年龄,邮编,病种"):
    lines = [header] + [",".join(map(str, r)) for r in rows]
    return ("\n".join(lines)).encode("utf-8-sig")


def _group(prefix, age, zipc):
    return [(f"{prefix}{i}", age, zipc,
             "糖尿病" if i % 2 == 0 else "高血压") for i in range(3)]


VALID = _group("A", 40, "10000") + _group("B", 50, "10000")


def _published_blob(disease_rows):
    """构造一个已发布文件的字节（病种列可含换行，引号闭合、可解析）。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([AGE, ZIP, DISEASE])
    for age, zipc, disease in disease_rows:
        w.writerow([age, zipc, disease])
    return buf.getvalue().encode("utf-8-sig")


def _insert_published(db_file, blob, filename="legacy.csv"):
    with db.connect(db_file) as conn:
        cur = conn.execute(
            "INSERT INTO batches (status, source_filename, row_count, "
            "output_csv, output_filename) "
            "VALUES ('published', ?, 3, ?, 'anonymous_batch_1.csv')",
            (filename, blob))
        return int(cur.lastrowid)


def _restart(client):
    """模拟重启：重新加载应用模块并触发 lifespan 启动核查。"""
    import app.main as main
    importlib.reload(main)
    return TestClient(main.app)


# --------------------------------------------------------------------------
# 解析层：引号不完整 / 病种跨行 -> 整批拒收
# --------------------------------------------------------------------------
def test_unclosed_quote_swallows_to_eof_rejected():
    payload = (
        "受试者号,年龄,邮编,病种\n"
        "S1,40,10008,糖尿病\n"
        "S2,41,10008,\"高血压\n"   # 引号未闭合，吞并后续所有记录
        "S3,42,10008,感冒\n"
    ).encode("utf-8")
    with pytest.raises(ValidationError, match="引号"):
        az.parse_csv(payload)


def test_quote_closed_on_next_record_still_rejected():
    # 引号在下一条记录处才闭合：病种吞并了 S3 整条记录（含受试者号）
    payload = (
        "受试者号,年龄,邮编,病种\n"
        "S1,40,10008,糖尿病\n"
        "S2,41,10008,\"高血压\n"
        "S3,42,10008,感冒\"\n"
    ).encode("utf-8")
    with pytest.raises(ValidationError, match="病种跨行"):
        az.parse_csv(payload)


def test_multiline_disease_closed_quote_rejected():
    payload = (
        "受试者号,年龄,邮编,病种\n"
        "S1,40,10008,糖尿病\n"
        "S2,41,10008,\"高血\n压\"\n"
        "S3,42,10008,感冒\n"
    ).encode("utf-8")
    with pytest.raises(ValidationError, match="病种跨行"):
        az.parse_csv(payload)


def test_quoted_single_line_disease_unchanged():
    # 单行病种（引号闭合、字段内含逗号）保持原有行为：正常解析
    payload = "受试者号,年龄,邮编,病种\nS1,40,10008,\"糖尿病,Ⅱ型\"\n".encode("utf-8")
    rows = az.parse_csv(payload)
    assert rows[0].disease == "糖尿病,Ⅱ型"


# --------------------------------------------------------------------------
# 输出层：跨行字段不得进入可下载文件
# --------------------------------------------------------------------------
def test_verify_output_rejects_linebreak_rows():
    rows = [{AGE: "40", ZIP: "10000", DISEASE: "高血压\nS3,42,10008,感冒"}]
    assert not az.verify_output(rows)


def test_render_output_csv_refuses_linebreak():
    rows = [{AGE: "40", ZIP: "10000", DISEASE: "高血压\nS3"}]
    with pytest.raises(ValueError, match="跨行"):
        az.render_output_csv(rows)


def test_find_linebreak_rows():
    rows = [
        {AGE: "40", ZIP: "10000", DISEASE: "糖尿病"},
        {AGE: "40", ZIP: "10000", DISEASE: "高血压\nS3,42,10008,感冒"},
    ]
    assert az.find_linebreak_rows(rows) == [2]
    assert az.find_linebreak_rows(rows[:1]) == []


# --------------------------------------------------------------------------
# 启动核查：已发布文件实际字节检查
# --------------------------------------------------------------------------
def test_inspect_clean_output_is_safe():
    blob = az.render_output_csv([{AGE: "40", ZIP: "10000", DISEASE: "糖尿病"}])
    assert az.inspect_downloadable_csv(blob) is None


def test_inspect_multiline_disease_flagged():
    blob = _published_blob([("40", "10000", "高血压\nS3,42,10000,感冒")])
    reason = az.inspect_downloadable_csv(blob)
    assert reason is not None and "换行" in reason


def test_inspect_unparseable_flagged():
    assert az.inspect_downloadable_csv(b"\xff\xfe\x00garbage") is not None
    assert az.inspect_downloadable_csv(b"") is not None
    assert az.inspect_downloadable_csv(None) is not None
    assert az.inspect_downloadable_csv("不是,表头\n1,2,3\n") is not None


# --------------------------------------------------------------------------
# Web：上传整批拒收
# --------------------------------------------------------------------------
def test_upload_unclosed_quote_rejected_entire_batch(client):
    c, _ = client
    payload = (
        "受试者号,年龄,邮编,病种\n"
        + "\n".join(",".join(map(str, r)) for r in VALID[:3])
        + "\nB9,50,10000,\"高血压\n"   # 未闭合引号吞并剩余记录
        + "\n".join(",".join(map(str, r)) for r in VALID[3:])
        + "\n"
    ).encode("utf-8")
    r = c.post("/upload", files={"file": ("bad.csv", payload, "text/csv")})
    assert r.status_code == 400
    assert "整批拒收" in r.text and "引号" in r.text
    assert "确认发布" not in c.get("/").text


def test_upload_multiline_disease_rejected(client):
    c, _ = client
    payload = (
        "受试者号,年龄,邮编,病种\n"
        "S1,40,10000,糖尿病\n"
        "S2,41,10000,\"高血\n压\"\n"
        "S3,42,10000,感冒\n"
    ).encode("utf-8")
    r = c.post("/upload", files={"file": ("bad.csv", payload, "text/csv")})
    assert r.status_code == 400
    assert "整批拒收" in r.text and "病种跨行" in r.text


# --------------------------------------------------------------------------
# Web：旧待确认预览在发布时拦截
# --------------------------------------------------------------------------
def _legacy_plan_with_swallowed_record():
    return {
        "age_level": 0, "zip_level": 0,
        "age_label": "原值", "zip_label": "原值",
        "level_sum": 0, "removed_count": 0, "removed_ids": [],
        "output_rows": [
            {AGE: "40", ZIP: "10000", DISEASE: "糖尿病"},
            {AGE: "40", ZIP: "10000", DISEASE: "高血压\nS3,42,10000,感冒"},
            {AGE: "40", ZIP: "10000", DISEASE: "感冒"},
        ],
        "groups": [{"age": "40", "zip": "10000", "count": 3,
                    "diseases": ["感冒", "糖尿病", "高血压\nS3,42,10000,感冒"]}],
    }


def test_publish_blocks_legacy_pending_preview(client):
    c, db_file = client
    with db.connect(db_file) as conn:
        bid = db.insert_pending(
            conn, "legacy.csv", 4, az.plan_to_json(_legacy_plan_with_swallowed_record()))
    # 首页明确提示已拦截，且不出现发布按钮
    home = c.get("/").text
    assert "已拦截" in home and "确认发布" not in home
    # 直接 POST 发布也被拦截
    r = c.post(f"/publish/{bid}", data={"confirm": "on"})
    assert r.status_code == 409
    assert "跨行病种" in r.text
    # 不产生任何可下载残件
    assert c.get(f"/download/{bid}").status_code == 404


# --------------------------------------------------------------------------
# Web：启动核查历史批次
# --------------------------------------------------------------------------
def test_startup_audit_marks_multiline_disease_batch(client):
    c, db_file = client
    blob = _published_blob([
        ("40", "10000", "糖尿病"),
        ("40", "10000", "高血压\nS3,42,10000,感冒"),
        ("40", "10000", "感冒"),
    ])
    bid = _insert_published(db_file, blob)

    with _restart(client) as c2:
        r = c2.get(f"/download/{bid}")
        assert r.status_code == 403
        assert "待核查" in r.text
        home = c2.get("/").text
        assert "待核查批次" in home and "换行" in home
        # 待核查批次不再出现在可下载列表
        assert f"/download/{bid}" not in home

    # 持久标记 + 原字节留存供内部核对
    with db.connect(db_file) as conn:
        row = db.get_batch(conn, bid)
        assert row["status"] == db.STATUS_REVIEW
        assert row["output_csv"] == blob
        assert row["review_reason"] and "换行" in row["review_reason"]
        assert row["review_marked_at"]

    # 再次重启：标记保持，不回退
    with _restart(client) as c3:
        assert c3.get(f"/download/{bid}").status_code == 403


def test_startup_audit_marks_unparseable_batch(client):
    c, db_file = client
    bid = _insert_published(db_file, b"\xff\xfe\x00not-a-csv")
    with _restart(client) as c2:
        assert c2.get(f"/download/{bid}").status_code == 403
    with db.connect(db_file) as conn:
        row = db.get_batch(conn, bid)
        assert row["status"] == db.STATUS_REVIEW
        assert "无法解析" in row["review_reason"]


def test_startup_audit_keeps_clean_batch_downloadable(client):
    c, db_file = client
    blob = _published_blob([
        ("40", "10000", "糖尿病"),
        ("40", "10000", "高血压"),
        ("40", "10000", "感冒"),
    ])
    bid = _insert_published(db_file, blob)
    with _restart(client) as c2:
        r = c2.get(f"/download/{bid}")
        assert r.status_code == 200
        assert r.content == blob  # 单行病种文件字节保持不变
    with db.connect(db_file) as conn:
        assert db.get_batch(conn, bid)["status"] == db.STATUS_PUBLISHED


def test_download_ordering_mark_committed_before_read(client):
    c, db_file = client
    c.post("/upload", files={"file": ("d.csv", _csv_bytes(VALID), "text/csv")})
    c.post("/publish/1", data={"confirm": "on"})
    # 标记提交前先取得字节的请求可以完成
    assert c.get("/download/1").status_code == 200
    # 标记提交之后再读取字节的下载不得放行
    with db.connect(db_file) as conn:
        assert db.mark_review(conn, 1, "启动核查：病种字段含换行")
    r = c.get("/download/1")
    assert r.status_code == 403
    assert "待核查" in r.text


def test_mark_review_only_from_published(client):
    c, db_file = client
    c.post("/upload", files={"file": ("d.csv", _csv_bytes(VALID), "text/csv")})
    with db.connect(db_file) as conn:
        # pending 批次不能被标记（仅 published -> review）
        assert not db.mark_review(conn, 1, "不应生效")
        assert db.get_batch(conn, 1)["status"] == db.STATUS_PENDING


# --------------------------------------------------------------------------
# 旧库迁移：CHECK 约束不含 review 的历史库
# --------------------------------------------------------------------------
def test_old_schema_migrated_and_audited(tmp_path, monkeypatch):
    db_file = tmp_path / "old.db"
    blob = _published_blob([("40", "10000", "高血压\nS3,42,10000,感冒")])
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE batches ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " status TEXT NOT NULL CHECK (status IN ('pending','published','rejected')),"
        " source_filename TEXT NOT NULL, row_count INTEGER NOT NULL,"
        " plan_json TEXT, output_csv BLOB, output_filename TEXT)")
    conn.execute(
        "INSERT INTO batches (status, source_filename, row_count, output_csv,"
        " output_filename) VALUES ('published','legacy.csv',3,?,'a.csv')", (blob,))
    conn.commit()
    conn.close()

    monkeypatch.setenv("ANON_DB_PATH", str(db_file))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    importlib.reload(main)
    with TestClient(main.app) as c:
        # 迁移 + 启动核查后：历史异常批次被拦截
        assert c.get("/download/1").status_code == 403
        # 迁移后完整流程仍可用（自增序列连续）
        c.post("/upload", files={"file": ("d.csv", _csv_bytes(VALID), "text/csv")})
        assert c.post("/publish/2", data={"confirm": "on"},
                      follow_redirects=False).status_code == 303
        assert c.get("/download/2").status_code == 200
    with db.connect(db_file) as conn2:
        row = db.get_batch(conn2, 1)
        assert row["status"] == db.STATUS_REVIEW
        assert row["output_csv"] == blob  # 原字节留存
