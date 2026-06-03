"""
Salary string normaliser.

Handles the salary_range free-text formats observed in production:
  "60000 - uncapped"        → (60000, None,  "EUR")
  "35000-45000"             → (35000, 45000, "EUR")
  "€55000 - €65000"         → (55000, 65000, "EUR")
  "from €50.000"            → (50000, None,  "EUR")   Italian thousands sep
  "€40,000 – €50,000"       → (40000, 50000, "EUR")   comma thousands sep
  "competitive" / garbage   → (None,  None,  None)

All amounts are returned in EUR equivalent.
No currency symbol = assume EUR (Italian B2B platform default).
"""

from __future__ import annotations

import re

from scoring.salary_benchmarks import FX_TO_EUR


def parse_salary(salary_range: str | None) -> tuple[float | None, float | None, str | None]:
    """
    Parse a free-text salary string.

    Returns:
        (min_eur, max_eur, original_currency)
        Any value may be None if it can't be determined.
        Never raises.
    """
    if not salary_range:
        return None, None, None

    s = salary_range.strip()

    # ── Detect currency ───────────────────────────────────────────────────────
    if "€" in s:
        currency = "EUR"
    elif "$" in s:
        currency = "USD"
    elif "£" in s:
        currency = "GBP"
    else:
        currency = "EUR"  # default for Italian platform

    # ── Strip symbols and noise words ─────────────────────────────────────────
    s = re.sub(r"[€$£]", "", s)
    s = re.sub(r"(?i)\bfrom\b", "", s)      # "from €50.000" → " 50.000"
    s = re.sub(r"(?i)\buncapped\b", "", s)  # "60000 - uncapped" → "60000 - "
    s = re.sub(r"(?i)\bnet\b|\bgrass\b|\byear\b|\bmonth\b|\bpa\b", "", s)

    # ── Italian thousands separator fix ──────────────────────────────────────
    # "50.000" means 50_000 in Italian; "50.5" is a decimal.
    # Rule: digit group of exactly 3 after a dot that is preceded by 1–3 digits.
    s = re.sub(r"(?<!\d)(\d{1,3})\.(\d{3})(?!\d)", r"\1\2", s)

    # ── Comma thousands separator → remove ────────────────────────────────────
    s = re.sub(r"(\d),(\d{3})(?!\d)", r"\1\2", s)

    # ── Extract numeric tokens ────────────────────────────────────────────────
    # Handles optional K/k suffix (e.g. "90K")
    nums: list[float] = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[kK]?", s):
        raw = m.group(0).strip()
        val_str = m.group(1)
        val = float(val_str)
        if raw.lower().endswith("k"):
            val *= 1000
        if val > 0:
            nums.append(val)

    if not nums:
        return None, None, None

    min_val: float | None = nums[0]
    max_val: float | None = nums[1] if len(nums) >= 2 else None

    # Sanity: if max < min (shouldn't happen but guard it) → swap
    if min_val is not None and max_val is not None and max_val < min_val:
        min_val, max_val = max_val, min_val

    # ── Convert to EUR ────────────────────────────────────────────────────────
    rate = FX_TO_EUR.get(currency, 1.0)
    min_eur = round(min_val * rate) if min_val is not None else None
    max_eur = round(max_val * rate) if max_val is not None else None

    return min_eur, max_eur, currency
