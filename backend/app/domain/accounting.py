"""Trial-balance CSV parsing ported from ``lib/accounting.ts`` (amounts in integer cents)."""
from __future__ import annotations

import re

from app.core.errors import BadRequest
from app.core.jsvalues import MAX_SAFE_INTEGER


def _rows(content: str) -> list[list[str]]:
    rows: list[list[str]] = []
    row: list[str] = []
    cell = ""
    quoted = False
    i = 0
    while i < len(content):
        char = content[i]
        if char == '"':
            if quoted and i + 1 < len(content) and content[i + 1] == '"':
                cell += '"'
                i += 1
            else:
                quoted = not quoted
        elif char == "," and not quoted:
            row.append(cell)
            cell = ""
        elif char == "\n" and not quoted:
            row.append(re.sub(r"\r\Z", "", cell))
            if any(v.strip() for v in row):
                rows.append(row)
            row = []
            cell = ""
        else:
            cell += char
        i += 1
    if quoted:
        raise BadRequest("CSV contains an unfinished quoted field.")
    row.append(re.sub(r"\r\Z", "", cell))
    if any(v.strip() for v in row):
        rows.append(row)
    return rows


def _amount(value: str | None, line: int) -> int:
    value = (value or "0").strip()
    if not re.fullmatch(r"\d+(\.\d{1,2})?", value, re.ASCII):
        raise BadRequest(f"Invalid amount on row {line}. Use positive amounts with up to two decimals.")
    whole, _, decimal = value.partition(".")
    amount = int(whole) * 100 + int(decimal.ljust(2, "0"))
    if amount > MAX_SAFE_INTEGER or amount > 1e13:
        raise BadRequest("Amount exceeds supported range.")
    return amount


def parse_trial_balance(content: str) -> dict:
    rows = _rows(content)
    header = rows.pop(0) if rows else []
    headers = [re.sub(r"^﻿", "", v.strip().lower()) for v in header]
    try:
        ai, di, ci = headers.index("account"), headers.index("debit"), headers.index("credit")
    except ValueError:
        raise BadRequest("CSV must contain account, debit and credit columns.") from None
    if not rows or len(rows) > 2000:
        raise BadRequest("Import between 1 and 2,000 account rows.")

    def cell(row: list[str], index: int) -> str | None:
        return row[index] if index < len(row) else None

    entries = []
    for index, row in enumerate(rows):
        account = (cell(row, ai) or "").strip()
        if not account:
            raise BadRequest(f"Account is missing on row {index + 2}.")
        entries.append({"account": account, "debit": _amount(cell(row, di), index + 2), "credit": _amount(cell(row, ci), index + 2)})
    debit = sum(e["debit"] for e in entries)
    credit = sum(e["credit"] for e in entries)
    if debit > MAX_SAFE_INTEGER or credit > MAX_SAFE_INTEGER:
        raise BadRequest("Total exceeds supported range.")
    return {"entries": entries, "debit": debit, "credit": credit, "difference": debit - credit, "balanced": debit == credit}
