"""CSV 解析、整批校验与 k-匿名方案搜索。

业务规则
--------
* 最多 30 行数据，列：受试者号、年龄(0~99)、五位邮编、病种。
* 受试者号重复 -> 整批拒收。
* 发布前移除受试者号；年龄隐藏等级：0 原值 / 1 十岁段 / 2 隐藏；
  邮编隐藏等级：0 原值 / 1 前三位 / 2 隐藏。
* 最多剔除 2 行；保留数据中每个 (年龄, 邮编) 组合至少 3 人且含至少 2 种病种。
* 选择两列隐藏等级之和最小的方案；同级先少剔除，再优先保留年龄精度。
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from itertools import combinations
from typing import Optional

MAX_ROWS = 30
MAX_REMOVE = 2
MIN_GROUP_SIZE = 3
MIN_DISEASE_KINDS = 2
HIDDEN = "***"

SUBJECT = "受试者号"
AGE = "年龄"
ZIP = "邮编"
DISEASE = "病种"
REQUIRED_HEADERS = (SUBJECT, AGE, ZIP, DISEASE)
OUTPUT_HEADERS = (AGE, ZIP, DISEASE)

# 兼容常见英文/别名表头
HEADER_ALIASES = {
    "受试者号": SUBJECT, "受试者编号": SUBJECT, "编号": SUBJECT,
    "subject_id": SUBJECT, "subjectid": SUBJECT, "id": SUBJECT,
    "年龄": AGE, "age": AGE,
    "邮编": ZIP, "邮政编码": ZIP, "zip": ZIP, "zipcode": ZIP, "postcode": ZIP,
    "病种": DISEASE, "疾病": DISEASE, "disease": DISEASE, "diagnosis": DISEASE,
}

AGE_LABELS = {0: "原值", 1: "十岁段", 2: "隐藏"}
ZIP_LABELS = {0: "原值", 1: "前三位", 2: "隐藏"}


class ValidationError(Exception):
    """CSV 结构或字段校验失败（整批拒收）。"""


@dataclass(frozen=True)
class Row:
    subject_id: str
    age: int
    zipcode: str
    disease: str


# --------------------------------------------------------------------------
# 解析与校验
# --------------------------------------------------------------------------
def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValidationError("文件编码无法识别，请使用 UTF-8 或 GBK 编码的 CSV 文件")


def parse_csv(data: bytes) -> list[Row]:
    """解析并整批校验上传的 CSV，失败时抛出 ValidationError。"""
    if not data or not data.strip():
        raise ValidationError("上传文件为空")

    text = decode_bytes(data)
    reader = csv.reader(io.StringIO(text))
    raw_lines = [line for line in reader if any(cell.strip() for cell in line)]
    if not raw_lines:
        raise ValidationError("CSV 文件内容为空")

    header = [h.strip().lstrip("﻿") for h in raw_lines[0]]
    col_index: dict[str, int] = {}
    for idx, name in enumerate(header):
        canonical = HEADER_ALIASES.get(name.lower())
        if canonical and canonical not in col_index:
            col_index[canonical] = idx
    missing = [h for h in REQUIRED_HEADERS if h not in col_index]
    if missing:
        raise ValidationError("缺少必需列：" + "、".join(missing) +
                              "（表头须包含：受试者号、年龄、邮编、病种）")

    data_lines = raw_lines[1:]
    if not data_lines:
        raise ValidationError("CSV 没有数据行")
    if len(data_lines) > MAX_ROWS:
        raise ValidationError(
            f"数据行数超过上限：最多 {MAX_ROWS} 行，实际 {len(data_lines)} 行，整批拒收")

    rows: list[Row] = []
    seen_ids: set[str] = set()
    for line_no, raw in enumerate(data_lines, start=2):
        if len(raw) < len(col_index):
            raw = raw + [""] * (len(col_index) - len(raw))

        def cell(col: str) -> str:
            return raw[col_index[col]].strip()

        sid = cell(SUBJECT)
        if not sid:
            raise ValidationError(f"第 {line_no} 行受试者号为空，整批拒收")
        if sid in seen_ids:
            raise ValidationError(f"受试者号重复：{sid}（第 {line_no} 行），整批拒收")
        seen_ids.add(sid)

        age_raw = cell(AGE)
        if not re.fullmatch(r"[0-9]+", age_raw):
            raise ValidationError(
                f"第 {line_no} 行年龄无效（需为 0～99 的整数）：{age_raw or '空'}，整批拒收")
        age = int(age_raw)
        if not 0 <= age <= 99:
            raise ValidationError(
                f"第 {line_no} 行年龄超出 0～99 范围：{age}，整批拒收")

        zip_raw = cell(ZIP)
        if not re.fullmatch(r"[0-9]{5}", zip_raw):
            raise ValidationError(
                f"第 {line_no} 行邮编无效（需为五位数字）：{zip_raw or '空'}，整批拒收")

        disease = cell(DISEASE)
        if not disease:
            raise ValidationError(f"第 {line_no} 行病种为空，整批拒收")

        rows.append(Row(subject_id=sid, age=age, zipcode=zip_raw, disease=disease))

    return rows


# --------------------------------------------------------------------------
# 匿名化变换与分组校验
# --------------------------------------------------------------------------
def age_value(age: int, level: int) -> str:
    if level == 0:
        return str(age)
    if level == 1:
        lo = (age // 10) * 10
        return f"{lo}-{lo + 9}"
    return HIDDEN


def zip_value(zipcode: str, level: int) -> str:
    if level == 0:
        return zipcode
    if level == 1:
        return zipcode[:3] + "**"
    return HIDDEN


def group_records(kept: list[Row], age_level: int, zip_level: int
                  ) -> dict[tuple[str, str], list[Row]]:
    groups: dict[tuple[str, str], list[Row]] = {}
    for r in kept:
        key = (age_value(r.age, age_level), zip_value(r.zipcode, zip_level))
        groups.setdefault(key, []).append(r)
    return groups


def groups_are_valid(groups: dict[tuple[str, str], list[Row]]) -> bool:
    for members in groups.values():
        if len(members) < MIN_GROUP_SIZE:
            return False
        if len({m.disease for m in members}) < MIN_DISEASE_KINDS:
            return False
    return True


def verify_output(output_rows: list[dict[str, str]]) -> bool:
    """发布前对已匿名化结果再次独立校验（纵深防御）。"""
    groups: dict[tuple[str, str], list[str]] = {}
    for r in output_rows:
        key = (r[AGE], r[ZIP])
        groups.setdefault(key, []).append(r[DISEASE])
    if not groups:
        return False
    for diseases in groups.values():
        if len(diseases) < MIN_GROUP_SIZE:
            return False
        if len(set(diseases)) < MIN_DISEASE_KINDS:
            return False
    return True


# --------------------------------------------------------------------------
# 方案搜索
# --------------------------------------------------------------------------
def _build_plan(rows: list[Row], age_level: int, zip_level: int,
                removed_idx: tuple[int, ...]) -> dict:
    removed_set = set(removed_idx)
    kept = [r for i, r in enumerate(rows) if i not in removed_set]
    groups = group_records(kept, age_level, zip_level)

    output_rows = [
        {
            AGE: age_value(r.age, age_level),
            ZIP: zip_value(r.zipcode, zip_level),
            DISEASE: r.disease,
        }
        for i, r in enumerate(rows) if i not in removed_set
    ]
    group_view = [
        {
            "age": key[0],
            "zip": key[1],
            "count": len(members),
            "diseases": sorted({m.disease for m in members}),
        }
        for key, members in sorted(groups.items())
    ]
    return {
        "age_level": age_level,
        "zip_level": zip_level,
        "age_label": AGE_LABELS[age_level],
        "zip_label": ZIP_LABELS[zip_level],
        "level_sum": age_level + zip_level,
        "removed_count": len(removed_idx),
        "removed_ids": [rows[i].subject_id for i in removed_idx],
        "output_rows": output_rows,
        "groups": group_view,
    }


def find_best_plan(rows: list[Row]) -> Optional[dict]:
    """枚举 9 种隐藏等级组合 × 至多剔除 2 行的所有方案，返回最优方案。

    排序键：(隐藏等级之和, 剔除数, 年龄隐藏等级, 被剔除编号元组)
    —— 末位仅用于在业务规则未区分时保证结果确定性。
    """
    n = len(rows)
    best_key: Optional[tuple] = None
    best_plan: Optional[dict] = None

    for age_level in (0, 1, 2):
        for zip_level in (0, 1, 2):
            for k in range(min(MAX_REMOVE, n) + 1):
                for removed_idx in combinations(range(n), k):
                    removed_set = set(removed_idx)
                    kept = [r for i, r in enumerate(rows) if i not in removed_set]
                    if not kept:
                        continue  # 不允许剔除全部行，空结果不是合法匿名方案
                    if not groups_are_valid(group_records(kept, age_level, zip_level)):
                        continue
                    removed_ids = tuple(rows[i].subject_id for i in removed_idx)
                    key = (age_level + zip_level, k, age_level, removed_ids)
                    if best_key is None or key < best_key:
                        best_key = key
                        best_plan = _build_plan(rows, age_level, zip_level, removed_idx)
    return best_plan


def render_output_csv(output_rows: list[dict[str, str]]) -> bytes:
    """将匿名结果渲染为 CSV 字节（UTF-8 BOM，便于 Excel 打开）。"""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(OUTPUT_HEADERS))
    writer.writeheader()
    for r in output_rows:
        writer.writerow({h: r[h] for h in OUTPUT_HEADERS})
    return buf.getvalue().encode("utf-8-sig")


def plan_to_json(plan: dict) -> str:
    return json.dumps(plan, ensure_ascii=False)


def plan_from_json(text: Optional[str]) -> Optional[dict]:
    return json.loads(text) if text else None
