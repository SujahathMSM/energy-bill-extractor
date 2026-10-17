"""Prompt text shared by every backend."""

import json

from .schema import extraction_json_schema

TOOL_NAME = "record_bill_fields"

SYSTEM_PROMPT = """\
You read electricity bills and record exactly what is printed on them.

Rules:
1. Copy values as printed. Do not convert units: if a rate is printed in pence, record the \
pence number and a unit such as "p/kWh".
2. If a field is not printed on the bill, use null or leave the field out. Never guess, and \
never calculate a value that is not printed. The one exception: give the ISO currency code \
implied by symbols (for example £ means GBP).
3. Write dates as YYYY-MM-DD.
4. Write numbers without currency symbols, units or thousands separators.
5. total_amount is this period's charges including tax and any other charge lines. If the \
bill also shows an amount due that includes a previous balance, payments or credits, do not \
use that figure.
6. Fill previous_reading and current_reading only for a single cumulative meter register. \
If the bill shows several registers (for example day and night) or no cumulative readings, \
use null for both.
7. Add one unit_rates entry for each unit rate charged, with the kWh charged at that rate \
when it is shown.
8. Fill period_days only if the bill prints the number of days; never count it yourself.
9. Put any other charge line for this period (levies, fuel or price adjustments, roundup \
donations, discounts) in other_charges with its amount as printed; use a negative amount for \
a credit. Do not put energy, standing charges, tax, previous balances or payments there.
10. If there are several pages, combine what they show into one answer.
"""


def build_user_prompt(pages: int, feedback: str | None = None, include_schema: bool = False) -> str:
    parts = [
        f"Record the fields from this electricity bill ({pages} page image"
        f"{'s' if pages != 1 else ''})."
    ]
    if include_schema:
        schema = json.dumps(extraction_json_schema(), separators=(",", ":"))
        parts.append(
            "Reply with a single JSON object that matches this JSON schema, and nothing else:\n"
            + schema
        )
    if feedback:
        parts.append(
            "Your previous answer did not pass validation. Fix these problems and answer "
            f"again:\n{feedback}"
        )
    return "\n\n".join(parts)
