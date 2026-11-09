# Banking source contract

This document defines which inputs the processing jobs must support. The repair
agent can request this contract through `read_repository_file` at the exact deployed
Git revision. It must read both this contract and the failed job's source before
the PR tool accepts a proposed repair. Source data supplies risk and KYC values;
the agent must not invent them.

## Delivery rules

- Files are UTF-8 CSV with a header and the columns in the order shown below.
- Files are stored under `raw/YYYY-MM-DD/` in the environment's data bucket.
- The initial customer file contains all customers. Later files contain new or
  changed profiles. Absence from a change file does not delete a customer.
- Batches run in date order, one workflow at a time. There is at most one distinct
  customer change per customer per batch. Historical corrections and concurrent
  batches are outside this implementation's scope.
- A batch can be rerun before a later customer batch is applied. An identical
  rerun adds neither customer versions nor transactions. Replaying older customer
  changes after a newer version exists is rejected.
- Required fields cannot be null or empty. Whitespace around fields is ignored.
- Duplicate identical rows are valid retransmissions. Conflicting rows for the
  same customer or transaction identity must stop processing.
- `batch_date` is a real calendar date formatted `YYYY-MM-DD`. All event dates
  below must equal that batch date after conversion to UTC.

## Customer file

```csv
customer_id,city,kyc_status,risk_rating,effective_at
C0001,Mumbai,VERIFIED,LOW,2026-10-28 00:00:00
```

| Field | Meaning and allowed values |
| --- | --- |
| `customer_id` | Stable, nonempty customer business key. Preserve leading zeros. |
| `city` | Nonempty city name; capitalization is meaningful. |
| `kyc_status` | `PENDING`, `VERIFIED`, or `EXPIRED`; comparison is case-insensitive. |
| `risk_rating` | `LOW`, `MEDIUM`, or `HIGH`; comparison is case-insensitive. |
| `effective_at` | The time the source says this profile became valid. |

**Both of these timestamp formats are valid and required:**

1. `2026-10-30 00:00:00`: a timezone-free value that represents UTC.
2. `2026-10-30T05:30:00+05:30`: ISO 8601 with a numeric timezone offset.
   A UTC `Z` suffix is also valid, for example `2026-10-30T00:00:00Z`.

The two example timestamps above represent the same instant. Preserve that
instant when normalizing to UTC. Fractional seconds are not part of this contract.

Track `city`, `kyc_status`, and `risk_rating` as SCD Type 2 attributes. Changes to
delivery metadata do not create a new version. Validity intervals are half-open:
`valid_from <= event_time < valid_to`; null `valid_to` means still current.
Two conflicting changes at the same effective time require source correction.

## Transaction file

```csv
transaction_id,customer_id,transaction_time,amount,currency,channel,status
T1-000001,C0001,2026-10-28 12:00:00,1250.50,INR,UPI,SUCCESS
```

| Field | Meaning and allowed values |
| --- | --- |
| `transaction_id` | Unique, immutable transaction identity. |
| `customer_id` | Customer business key with a valid history record at event time. |
| `transaction_time` | Either of the same timestamp formats accepted for customers. |
| `amount` | Positive decimal, up to 16 integer digits and two fractional digits. No separators or currency symbols. |
| `currency` | `INR`; multiple currencies are not aggregated by this project. |
| `channel` | `UPI`, `CARD`, or `NEFT`, case-insensitive. |
| `status` | `SUCCESS` or `FAILED`, case-insensitive. |

All transactions remain in the fact table. The report includes only `SUCCESS`
transactions. Reusing a loaded transaction ID with different values is a source
error, not an instruction to overwrite that transaction.

## Failure interpretation

Missing input, access failures, conflicting rows, unknown enumerations, impossible
dates, and out-of-order changes require source or operational correction. A parser
that rejects one of the valid timestamp representations above is a code defect.
If evidence does not establish the cause, request manual review. Never repair a
failure by dropping records, filling fabricated values, or bypassing validation.
