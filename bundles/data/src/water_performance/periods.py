"""Financial-year and price-control periods.

`report_year` is always the year in which a financial year ends: `2024-25` -> 2025. Calendar-year
facts keep a separate `calendar_year` and are never relabelled as financial years.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FY = re.compile(r"^\s*(?:FY\s*)?(\d{4})\s*[-/– _]\s*(\d{2}|\d{4})\s*$", re.IGNORECASE)

# Price controls by financial-year end. AMP6 = PR14, AMP7 = PR19, AMP8 = PR24.
_PRICE_CONTROLS = (
    ("AMP6", "PR14", 2016, 2020),
    ("AMP7", "PR19", 2021, 2025),
    ("AMP8", "PR24", 2026, 2030),
)


class PeriodError(ValueError):
    """A period label cannot be read as a financial year."""


@dataclass(frozen=True)
class FinancialYear:
    report_year: int  # year the financial year ends

    @property
    def label(self) -> str:
        return f"{self.report_year - 1}-{str(self.report_year)[-2:]}"

    @property
    def amp(self) -> str | None:
        return price_control(self.report_year)[0]

    @property
    def price_review(self) -> str | None:
        return price_control(self.report_year)[1]


def parse_financial_year(label) -> FinancialYear:
    """Read '2024-25', '2024/25', '2024-2025', '2024 2025' or 'FY2024-25' as report_year 2025.

    A bare four-digit year is ambiguous and rejected: callers must say whether it is a
    financial-year end or a calendar year."""
    if label is None:
        raise PeriodError("period is empty")
    text = str(label).strip()
    m = _FY.match(text)
    if not m:
        raise PeriodError(f"{text!r} is not a financial year label such as 2024-25")
    start = int(m.group(1))
    end_raw = m.group(2)
    if len(end_raw) == 4:
        end = int(end_raw)
    else:  # two digits: the next year with those last digits (1999-00 ends in 2000)
        end = start - start % 100 + int(end_raw)
        if end <= start:
            end += 100
    if end != start + 1:
        raise PeriodError(f"{text!r} does not span consecutive years")
    return FinancialYear(end)


def price_control(report_year: int) -> tuple[str | None, str | None]:
    for amp, review, first, last in _PRICE_CONTROLS:
        if first <= report_year <= last:
            return amp, review
    return None, None


def amp_years(amp: str) -> range:
    for name, _, first, last in _PRICE_CONTROLS:
        if name == amp:
            return range(first, last + 1)
    raise PeriodError(f"unknown AMP {amp!r}")


def period_rows(first: int = 2011, last: int = 2030) -> list[dict]:
    rows = []
    for year in range(first, last + 1):
        fy = FinancialYear(year)
        rows.append(
            {
                "report_year": year,
                "financial_year": fy.label,
                "fy_start_date": f"{year - 1}-04-01",
                "fy_end_date": f"{year}-03-31",
                "amp": fy.amp,
                "price_review": fy.price_review,
                "period_basis": "financial_year",
            }
        )
    return rows
