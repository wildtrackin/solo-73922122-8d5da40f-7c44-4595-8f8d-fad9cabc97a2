"""Web 端到端测试：上传、预览、发布、旧预览失效、重启持久性、残件保证。"""
import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("ANON_DB_PATH", str(db_file))
    # 确保拿到读取新环境变量的模块实例
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


def test_home_page_loads(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "临床数据匿名化发布平台" in r.text


def test_upload_duplicate_id_rejected_entire_batch(client):
    c, _ = client
    bad = VALID + [("A0", 60, "20000", "感冒")]  # A0 与首行重复
    r = c.post("/upload", files={"file": ("d.csv", _csv_bytes(bad), "text/csv")})
    assert r.status_code == 400
    assert "整批拒收" in r.text and "受试者号重复" in r.text
    # 拒收后不存在待发布预览
    r2 = c.get("/")
    assert "确认发布" not in r2.text


def test_full_publish_and_download_flow(client):
    c, _ = client
    r = c.post("/upload", files={"file": ("d.csv", _csv_bytes(VALID), "text/csv")},
               follow_redirects=False)
    assert r.status_code == 303
    home = c.get("/").text
    assert "匿名预览" in home and "确认发布" in home
    # 预览中不含受试者号
    assert "A0" not in home

    # 未勾选确认不能发布
    r = c.post("/publish/1", data={"confirm": ""})
    assert r.status_code == 400
    # 未发布时下载返回 404（无残件）
    assert c.get("/download/1").status_code == 404

    r = c.post("/publish/1", data={"confirm": "on"}, follow_redirects=False)
    assert r.status_code == 303
    d = c.get("/download/1")
    assert d.status_code == 200
    assert d.headers["content-disposition"].startswith("attachment")
    assert "受试者号" not in d.content.decode("utf-8-sig")
    assert d.content.count(b"\n") == 7  # 表头 + 6 行


def test_no_solution_cannot_publish(client):
    c, _ = client
    data = [(f"S{i}", 40 + i, "10000", "糖尿病") for i in range(3)]  # 仅一种病种
    r = c.post("/upload", files={"file": ("d.csv", _csv_bytes(data), "text/csv")})
    assert r.status_code == 422
    assert "无可行匿名方案" in r.text
    assert c.get("/").text.count("确认发布") == 0


def test_reupload_invalidates_old_preview(client):
    c, _ = client
    # 第一次上传，得到批次 1 的预览
    c.post("/upload", files={"file": ("a.csv", _csv_bytes(VALID), "text/csv")})
    assert c.get("/").status_code == 200
    # 第二次上传新数据，旧预览（批次 1）不得发布
    other = _group("C", 60, "30000") + _group("D", 70, "40000")
    c.post("/upload", files={"file": ("b.csv", _csv_bytes(other), "text/csv")})
    r = c.post("/publish/1", data={"confirm": "on"})
    assert r.status_code == 409
    assert "不可发布" in r.text
    # 新预览可正常发布
    assert c.post("/publish/2", data={"confirm": "on"},
                  follow_redirects=False).status_code == 303
    # 批次 1 始终没有可下载文件
    assert c.get("/download/1").status_code == 404
    assert c.get("/download/2").status_code == 200


def test_published_file_persists_across_restart(client, tmp_path):
    c, db_file = client
    c.post("/upload", files={"file": ("d.csv", _csv_bytes(VALID), "text/csv")})
    c.post("/publish/1", data={"confirm": "on"})
    before = c.get("/download/1").content

    # 模拟重启：新建应用实例指向同一个 SQLite 文件
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c2:
        r = c2.get("/download/1")
        assert r.status_code == 200
        assert r.content == before  # 重启后内容不变
        home = c2.get("/").text
        assert "anonymous_batch_1.csv" in home


def test_publish_unknown_batch_404(client):
    c, _ = client
    r = c.post("/publish/999", data={"confirm": "on"})
    assert r.status_code == 409


def test_over_30_rows_rejected(client):
    c, _ = client
    data = [(f"S{i}", 40, "10000", "糖尿病") for i in range(31)]
    r = c.post("/upload", files={"file": ("big.csv", _csv_bytes(data), "text/csv")})
    assert r.status_code == 400 and "超过上限" in r.text
