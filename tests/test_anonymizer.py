"""匿名化引擎单元测试。运行：python -m pytest tests/ -v"""
import pytest

from app import anonymizer as az
from app.anonymizer import Row, ValidationError, AGE, ZIP, DISEASE


def _csv(rows: list[tuple], header="受试者号,年龄,邮编,病种") -> bytes:
    lines = [header] + [",".join(map(str, r)) for r in rows]
    return ("\n".join(lines)).encode("utf-8-sig")


# ------------------------- 解析与整批校验 -------------------------
def test_parse_valid_minimal():
    rows = az.parse_csv(_csv([("S1", 42, "10008", "糖尿病")]))
    assert rows == [Row("S1", 42, "10008", "糖尿病")]


def test_duplicate_subject_id_rejected():
    data = _csv([("S1", 42, "10008", "糖尿病"), ("S1", 50, "10008", "高血压")])
    with pytest.raises(ValidationError, match="受试者号重复"):
        az.parse_csv(data)


def test_row_limit_enforced():
    data = _csv([(f"S{i}", 40, "10000", "糖尿病") for i in range(31)])
    with pytest.raises(ValidationError, match="超过上限"):
        az.parse_csv(data)


@pytest.mark.parametrize("age", ["-1", "100", "abc", ""])
def test_bad_age(age):
    with pytest.raises(ValidationError, match="年龄"):
        az.parse_csv(_csv([("S1", age, "10008", "糖尿病")]))


@pytest.mark.parametrize("zipc", ["1234", "123456", "abcde", "1234x", ""])
def test_bad_zip(zipc):
    with pytest.raises(ValidationError, match="邮编"):
        az.parse_csv(_csv([("S1", 40, zipc, "糖尿病")]))


def test_missing_header_rejected():
    with pytest.raises(ValidationError, match="缺少必需列"):
        az.parse_csv(_csv([("S1", 40, "10008")], header="受试者号,年龄,邮编"))


def test_empty_disease_rejected():
    with pytest.raises(ValidationError, match="病种为空"):
        az.parse_csv(_csv([("S1", 40, "10008", " ")]))


def test_english_header_alias_and_gbk():
    text = "id,age,zip,disease\nS1,40,10081,糖尿病\n"
    rows = az.parse_csv(text.encode("gbk"))
    assert rows[0].subject_id == "S1" and rows[0].age == 40


# ------------------------- 变换函数 -------------------------
def test_age_band():
    assert az.age_value(0, 1) == "0-9"
    assert az.age_value(42, 1) == "40-49"
    assert az.age_value(99, 1) == "90-99"
    assert az.age_value(42, 2) == "***"
    assert az.age_value(42, 0) == "42"


def test_zip_prefix():
    assert az.zip_value("10008", 1) == "100**"
    assert az.zip_value("10008", 2) == "***"


# ------------------------- 方案搜索：无解 -------------------------
def test_no_solution_too_few_rows():
    # 仅 2 行，任何分组都达不到 3 人
    rows = az.parse_csv(_csv([
        ("S1", 40, "10000", "糖尿病"),
        ("S2", 41, "10000", "高血压"),
    ]))
    assert az.find_best_plan(rows) is None


def test_no_solution_single_disease():
    # 3 人同组但只有一种病种
    rows = az.parse_csv(_csv([
        (f"S{i}", 40 + i, "10000", "糖尿病") for i in range(3)
    ]))
    assert az.find_best_plan(rows) is None


# ------------------------- 方案搜索：可行方案 -------------------------
def _group_of_three(sid_prefix, age, zipc, diseases):
    return [(f"{sid_prefix}{i}", age, zipc, diseases[i % len(diseases)])
            for i in range(3)]


def test_best_plan_minimal_hiding_level_zero():
    data = (_group_of_three("A", 40, "10000", ["糖尿病", "高血压", "糖尿病"])
            + _group_of_three("B", 50, "10000", ["感冒", "高血压", "感冒"]))
    rows = az.parse_csv(_csv(data))
    plan = az.find_best_plan(rows)
    assert plan is not None
    assert plan["age_level"] == 0 and plan["zip_level"] == 0
    assert plan["removed_count"] == 0
    assert all(AGE in r and ZIP in r and DISEASE in r for r in plan["output_rows"])
    assert all("受试者号" not in r for r in plan["output_rows"])


def test_age_banding_solves_sparse_ages():
    # 12 人，年龄 40~45 各 2 人、同邮编 -> 原值时每年龄仅 2 人不达标；
    # 十岁段后 40-49 共 12 人且两种病种，无需剔除
    data = [(f"S{i:02d}", 40 + (i % 6), "10001",
             "糖尿病" if i % 2 == 0 else "高血压") for i in range(12)]
    rows = az.parse_csv(_csv(data))
    plan = az.find_best_plan(rows)
    assert plan is not None
    assert plan["age_level"] == 1
    assert plan["zip_level"] == 0
    assert plan["removed_count"] == 0
    assert all(r[AGE] == "40-49" for r in plan["output_rows"])
    assert az.verify_output(plan["output_rows"])


def test_removal_at_most_two():
    # 一个合规 3 人组 + 两个“落单”行；剔除 2 行后可行
    data = _group_of_three("A", 40, "10000", ["糖尿病", "高血压", "糖尿病"])
    data += [("X1", 99, "99999", "罕见病"), ("X2", 88, "88888", "罕见病")]
    rows = az.parse_csv(_csv(data))
    plan = az.find_best_plan(rows)
    assert plan is not None
    assert plan["removed_count"] == 2
    assert set(plan["removed_ids"]) == {"X1", "X2"}
    assert len(plan["output_rows"]) == 3


def test_cannot_remove_more_than_two():
    # 10 行全部为同一病种：任何隐藏等级（含全隐藏合并）下，每个组都只有
    # 一种病种，无法满足“至少两种病种”。唯一出路是剔除，但至少要剔除到
    # 无法构成任何组（≤2 行）才行，即最少剔除 8 行，远超上限 2 -> 无解
    data = [(f"S{i:02d}", 40 + (i % 6), "1000" + str(i % 5 + 1), "糖尿病")
            for i in range(10)]
    rows = az.parse_csv(_csv(data))
    assert az.find_best_plan(rows) is None


def test_tie_break_prefers_age_precision():
    # 每个前三位邮编下有 3 个不同五位邮编各 1 人（年龄同为 40、含两种病种）：
    # 和=0 时邮编组只有 1 人不可行；和=1 时 (age0,zip1) 与 (age1,zip0) 均可行
    # -> 按规则优先保留年龄精度 (0,1)
    rows_data = []
    sid = 0
    for prefix in ("100", "200"):
        for suffix, disease in (("01", "糖尿病"), ("02", "高血压"), ("03", "糖尿病")):
            sid += 1
            rows_data.append((f"S{sid}", 40, prefix + suffix, disease))
    rows = az.parse_csv(_csv(rows_data))
    plan = az.find_best_plan(rows)
    assert plan is not None
    assert plan["level_sum"] == 1
    assert plan["age_level"] == 0 and plan["zip_level"] == 1


def test_tie_break_prefers_fewer_removals():
    # 同一隐藏等级下，0 剔除方案优先于需要剔除的方案
    data = _group_of_three("A", 40, "10000", ["糖尿病", "高血压", "糖尿病"])
    data += _group_of_three("B", 50, "10000", ["感冒", "高血压", "感冒"])
    data += [("X1", 40, "10000", "糖尿病")]  # 加入 A 组仍合规，无需剔除
    rows = az.parse_csv(_csv(data))
    plan = az.find_best_plan(rows)
    assert plan["removed_count"] == 0


def test_render_and_reverify_csv_roundtrip():
    import csv
    import io
    data = _group_of_three("A", 40, "10000", ["糖尿病", "高血压", "糖尿病"])
    rows = az.parse_csv(_csv(data))
    plan = az.find_best_plan(rows)
    blob = az.render_output_csv(plan["output_rows"])
    parsed = list(csv.DictReader(io.StringIO(blob.decode("utf-8-sig"))))
    assert list(parsed[0].keys()) == [AGE, ZIP, DISEASE]
    assert len(parsed) == 3
    assert az.verify_output(plan["output_rows"])
