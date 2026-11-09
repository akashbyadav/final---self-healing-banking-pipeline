"""Load immutable transactions and ensure each has a historical customer version."""

import re
import sys
from datetime import date

import boto3
from awsglue.context import GlueContext
from awsglue.utils import getResolvedOptions
from botocore.exceptions import ClientError
from pyspark.context import SparkContext
from pyspark.sql import functions as F
from pyspark.sql.types import StringType, StructField, StructType


def require(condition, message):
    """Reject unusable source records without silently dropping transaction value."""
    if not condition:
        raise ValueError(message)


def main():
    """Normalize a daily file, check its customer references, and insert new IDs."""
    args = getResolvedOptions(
        sys.argv,
        ["JOB_NAME", "bucket", "database", "input_key", "batch_date", "source_commit"],
    )
    require(re.fullmatch(r"[a-z][a-z0-9_]*", args["database"]), "Invalid database name")
    require(
        re.fullmatch(r"\d{4}-\d{2}-\d{2}", args["batch_date"]), "Invalid batch date"
    )
    # Reject impossible calendar dates before Spark compares event dates to the batch.
    try:
        date.fromisoformat(args["batch_date"])
    except ValueError as error:
        raise ValueError(
            "SOURCE_DATA_ERROR: batch_date is not a calendar date"
        ) from error
    require(args["input_key"].startswith("raw/"), "Input must be under raw/")

    # A missing file is an upstream delivery issue, not an empty transaction batch.
    try:
        boto3.client("s3").head_object(Bucket=args["bucket"], Key=args["input_key"])
    except ClientError as error:
        if error.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            raise FileNotFoundError(f"SOURCE_MISSING: {args['input_key']}") from error
        raise

    context = GlueContext(SparkContext.getOrCreate())
    spark = context.spark_session
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    spark.conf.set("spark.sql.ansi.enabled", "false")
    spark.conf.set("spark.sql.legacy.timeParserPolicy", "CORRECTED")

    # Read all input fields as text, then apply explicit business types below.
    columns = [
        "transaction_id",
        "customer_id",
        "transaction_time",
        "amount",
        "currency",
        "channel",
        "status",
    ]
    schema = StructType([StructField(name, StringType(), True) for name in columns])
    source = (
        spark.read.option("header", True)
        .option("enforceSchema", False)
        .option("mode", "FAILFAST")
        .schema(schema)
        .csv(f"s3://{args['bucket']}/{args['input_key']}")
    )
    source = source.select(*[F.trim(F.col(name)).alias(name) for name in columns])
    require(not source.rdd.isEmpty(), "SOURCE_DATA_ERROR: transaction file is empty")
    missing = F.lit(False)
    for name in columns:
        missing = missing | F.col(name).isNull() | (F.col(name) == "")
    require(
        not source.filter(missing).take(1),
        "SOURCE_DATA_ERROR: missing transaction field",
    )

    # Reject excess decimal places before casting, rather than silently rounding money.
    require(
        not source.filter(~F.col("amount").rlike(r"^[0-9]{1,16}(\.[0-9]{1,2})?$")).take(
            1
        ),
        "SOURCE_DATA_ERROR: amount must be a plain decimal with at most two decimal places",
    )
    # Decimal money avoids floating point rounding. Both timestamp formats are valid.
    source = source.withColumn("amount", F.col("amount").cast("decimal(18,2)"))
    source = source.withColumn(
        "transaction_time",
        F.coalesce(
            F.to_timestamp("transaction_time", "yyyy-MM-dd HH:mm:ss"),
            F.to_timestamp("transaction_time", "yyyy-MM-dd'T'HH:mm:ssXXX"),
        ),
    )
    for name in ("currency", "channel", "status"):
        source = source.withColumn(name, F.upper(name))
    invalid = (
        F.col("amount").isNull()
        | (F.col("amount") <= 0)
        | F.col("transaction_time").isNull()
        | (F.col("currency") != "INR")
        | ~F.col("channel").isin("UPI", "CARD", "NEFT")
        | ~F.col("status").isin("SUCCESS", "FAILED")
    )
    require(
        not source.filter(invalid).take(1),
        "SOURCE_DATA_ERROR: invalid transaction value",
    )
    require(
        not source.filter(
            F.to_date("transaction_time") != F.lit(args["batch_date"])
        ).take(1),
        "SOURCE_DATA_ERROR: transaction UTC date must equal batch_date",
    )

    # Identical repeats collapse to one record; conflicting versions of an immutable
    # transaction ID are rejected instead of being overwritten.
    source = source.dropDuplicates().cache()
    require(
        not source.groupBy("transaction_id").count().filter("count > 1").take(1),
        "SOURCE_DATA_ERROR: conflicting rows for a transaction_id",
    )
    dimension = spark.table(f"glue_catalog.{args['database']}.dim_customer")
    condition = (
        (F.col("f.customer_id") == F.col("d.customer_id"))
        & (F.col("f.transaction_time") >= F.col("d.valid_from"))
        & (
            F.col("d.valid_to").isNull()
            | (F.col("f.transaction_time") < F.col("d.valid_to"))
        )
    )
    unmatched = source.alias("f").join(dimension.alias("d"), condition, "left_anti")
    require(
        not unmatched.take(1),
        "SOURCE_DATA_ERROR: transaction has no historical customer version",
    )

    # The fact stores the customer business key and event time. Athena uses those
    # fields for an as-of join, so historical risk classifications stay meaningful.
    table = f"glue_catalog.{args['database']}.fact_transactions"
    spark.sql(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            transaction_id STRING, customer_id STRING, transaction_time TIMESTAMP,
            amount DECIMAL(18,2), currency STRING, channel STRING,
            status STRING, source_batch STRING
        ) USING iceberg PARTITIONED BY (days(transaction_time))
        LOCATION 's3://{args["bucket"]}/warehouse/fact_transactions'
        TBLPROPERTIES ('format-version'='2')
    """)
    existing = spark.table(table)
    repeated = source.alias("s").join(existing.alias("t"), "transaction_id", "inner")
    different = F.lit(False)
    for name in columns[1:]:
        different = different | ~F.col(f"s.{name}").eqNullSafe(F.col(f"t.{name}"))
    require(
        not repeated.filter(different).take(1),
        "SOURCE_DATA_ERROR: existing transaction was changed",
    )

    # Only new IDs are inserted. A rerun therefore cannot double-count transactions.
    source.withColumn(
        "source_batch", F.lit(args["batch_date"])
    ).createOrReplaceTempView("transaction_batch")
    spark.sql(f"""
        MERGE INTO {table} t USING transaction_batch s
        ON t.transaction_id = s.transaction_id
        WHEN NOT MATCHED THEN INSERT *
    """)
    print(
        f"TRANSACTION_SUCCESS batch={args['batch_date']} rows={source.count()} commit={args['source_commit']}"
    )
    source.unpersist()


if __name__ == "__main__":
    main()
