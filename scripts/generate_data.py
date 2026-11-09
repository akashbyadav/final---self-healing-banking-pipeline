"""Generate deterministic synthetic banking deliveries without external libraries."""

import argparse
import csv
import json
import random
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path


def write_csv(path, fields, rows):
    """Write a UTF-8 CSV with stable field order and proper quoting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def generate(output):
    """Create three ordered batches, duplicate deliveries, and one conflict file."""
    rng = random.Random(42)
    customer_fields = [
        "customer_id",
        "city",
        "kyc_status",
        "risk_rating",
        "effective_at",
    ]
    transaction_fields = [
        "transaction_id",
        "customer_id",
        "transaction_time",
        "amount",
        "currency",
        "channel",
        "status",
    ]
    customers = {}
    expected = {"batches": [], "total_transactions": 0, "total_customer_versions": 0}

    # IDs and profiles are invented. The same seed makes every deployment reproducible.
    for number in range(1, 1001):
        customers[number] = {
            "customer_id": f"C{number:04d}",
            "city": ["Mumbai", "Pune", "Delhi", "Bengaluru"][number % 4],
            "kyc_status": "PENDING" if number % 5 == 0 else "VERIFIED",
            "risk_rating": "LOW",
        }

    # Daily changes are effective at UTC midnight. Batch three expresses that same
    # instant as 05:30 at +05:30 to exercise the documented timezone-aware format.
    for index, count in enumerate((3000, 3500, 3500)):
        day = datetime(2026, 10, 28) + timedelta(days=index)
        batch = day.strftime("%Y-%m-%d")
        changed_ids = list(customers) if index == 0 else list(range(1, 101))
        rows = []
        for number in changed_ids:
            if index:
                customers[number]["risk_rating"] = "MEDIUM" if index == 1 else "HIGH"
                customers[number]["kyc_status"] = "VERIFIED"
            effective = f"{batch} 00:00:00" if index < 2 else f"{batch}T05:30:00+05:30"
            rows.append({**customers[number], "effective_at": effective})
        write_csv(output / batch / "customers.csv", customer_fields, rows)

        # Every transaction has a positive INR amount. Exact duplicates at the end
        # simulate a retransmitted delivery; they must not increase the fact count.
        transactions = []
        successful_total = Decimal("0.00")
        successful_count = 0
        for number in range(count):
            status = "FAILED" if number % 11 == 0 else "SUCCESS"
            amount = Decimal(rng.randint(100, 500000)) / 100
            transactions.append(
                {
                    "transaction_id": f"T{index + 1}-{number + 1:06d}",
                    "customer_id": f"C{rng.randint(1, 1000):04d}",
                    "transaction_time": (
                        day + timedelta(seconds=3600 + number * 20)
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                    "amount": f"{amount:.2f}",
                    "currency": "INR",
                    "channel": rng.choice(["UPI", "CARD", "NEFT"]),
                    "status": status,
                }
            )
            if status == "SUCCESS":
                successful_total += amount
                successful_count += 1
        write_csv(
            output / batch / "transactions.csv",
            transaction_fields,
            transactions + transactions[:10],
        )

        # This separate file is intentionally invalid source data, not a parser bug.
        if index == 1:
            conflict = {**rows[0], "risk_rating": "HIGH"}
            write_csv(
                output / batch / "customers-conflict.csv",
                customer_fields,
                rows + [conflict],
            )
        expected["total_transactions"] += count
        expected["total_customer_versions"] += len(rows)
        expected["batches"].append(
            {
                "batch_date": batch,
                "new_transactions": count,
                "successful_transactions": successful_count,
                "successful_amount_inr": str(successful_total),
                "cumulative_transactions": expected["total_transactions"],
                "cumulative_customer_versions": expected["total_customer_versions"],
            }
        )

    # This manifest supplies exact values for manual comparison in Athena.
    (output / "expected-results.json").write_text(json.dumps(expected, indent=2) + "\n")
    print(json.dumps(expected, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/generated"))
    generate(parser.parse_args().output)
