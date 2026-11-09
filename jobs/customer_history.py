"""Maintain customer SCD2 history from an ordered batch of profile changes."""

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
    """Stop the job with a readable explanation when a source rule is broken."""
    if not condition:
        raise ValueError(message)


def parse_effective_time(column):
    """Read both timestamp formats in the source contract and normalize to UTC."""
    # The timezone-free representation is already UTC. The second representation
    # includes an offset; Spark converts it to UTC because the session uses UTC.
    return F.coalesce(
        F.to_timestamp(column, "yyyy-MM-dd HH:mm:ss"),
        F.to_timestamp(column, "yyyy-MM-dd'T'HH:mm:ssXXX"),
    )


def main():
    """Read one input file, identify real changes, and commit all SCD2 actions."""
    args = getResolvedOptions(
        sys.argv,
        ["JOB_NAME", "bucket", "database", "input_key", "batch_date", "source_commit"],
    )
    require(re.fullmatch(r"[a-z][a-z0-9_]*", args["database"]), "Invalid database name")
    require(
        re.fullmatch(r"\d{4}-\d{2}-\d{2}", args["batch_date"]), "Invalid batch date"
    )
    # Check the calendar date too; a date-shaped string such as February 30 is invalid.
    try:
        date.fromisoformat(args["batch_date"])
    except ValueError as error:
        raise ValueError(
            "SOURCE_DATA_ERROR: batch_date is not a calendar date"
        ) from error
    require(args["input_key"].startswith("raw/"), "Input must be under raw/")

    # Check the exact object first, so a missing delivery has a clear diagnosis.
    s3 = boto3.client("s3")
    try:
        s3.head_object(Bucket=args["bucket"], Key=args["input_key"])
    except ClientError as error:
        if error.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            raise FileNotFoundError(f"SOURCE_MISSING: {args['input_key']}") from error
        raise

    # Infrastructure supplies the Iceberg catalog configuration to this session.
    context = GlueContext(SparkContext.getOrCreate())
    spark = context.spark_session
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    spark.conf.set("spark.sql.ansi.enabled", "false")
    spark.conf.set("spark.sql.legacy.timeParserPolicy", "CORRECTED")

    # An explicit schema keeps customer IDs and timestamps from being inferred.
    columns = ["customer_id", "city", "kyc_status", "risk_rating", "effective_at"]
    schema = StructType([StructField(name, StringType(), True) for name in columns])
    incoming = (
        spark.read.option("header", True)
        .option("enforceSchema", False)
        .option("mode", "FAILFAST")
        .schema(schema)
        .csv(f"s3://{args['bucket']}/{args['input_key']}")
    )
    incoming = incoming.select(*[F.trim(F.col(name)).alias(name) for name in columns])
    require(not incoming.rdd.isEmpty(), "SOURCE_DATA_ERROR: customer file is empty")

    # Null or empty attributes cannot define a usable historical customer record.
    missing = F.lit(False)
    for name in columns:
        missing = missing | F.col(name).isNull() | (F.col(name) == "")
    require(
        not incoming.filter(missing).take(1),
        "SOURCE_DATA_ERROR: missing customer field",
    )

    # Canonical values prevent harmless casing differences from creating versions.
    incoming = incoming.withColumn("kyc_status", F.upper("kyc_status")).withColumn(
        "risk_rating", F.upper("risk_rating")
    )
    invalid = (~F.col("kyc_status").isin("PENDING", "VERIFIED", "EXPIRED")) | (
        ~F.col("risk_rating").isin("LOW", "MEDIUM", "HIGH")
    )
    require(
        not incoming.filter(invalid).take(1),
        "SOURCE_DATA_ERROR: unknown KYC or risk value",
    )

    # Identical retransmitted rows are harmless. Conflicting updates need the source
    # owner to resolve them; choosing one arbitrarily would rewrite business history.
    incoming = incoming.dropDuplicates()
    conflicts = incoming.groupBy("customer_id").count().filter("count > 1")
    require(
        not conflicts.take(1), "SOURCE_DATA_ERROR: multiple updates for one customer"
    )
    incoming = incoming.withColumn(
        "effective_time", parse_effective_time(F.col("effective_at"))
    )
    require(
        not incoming.filter("effective_time IS NULL").take(1),
        "CONTRACT_PARSE_ERROR: effective_at could not be parsed; see docs/source-contract.md",
    )
    require(
        not incoming.filter(
            F.to_date("effective_time") != F.lit(args["batch_date"])
        ).take(1),
        "SOURCE_DATA_ERROR: effective_at UTC date must equal batch_date",
    )

    # Hash only tracked business attributes. Arrival time and batch ID are not changes.
    incoming = incoming.withColumn(
        "attribute_hash",
        F.sha2(F.to_json(F.struct("city", "kyc_status", "risk_rating")), 256),
    ).cache()
    table = f"glue_catalog.{args['database']}.dim_customer"
    spark.sql(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            customer_version_id STRING, customer_id STRING, city STRING,
            kyc_status STRING, risk_rating STRING, attribute_hash STRING,
            valid_from TIMESTAMP, valid_to TIMESTAMP, is_current BOOLEAN,
            source_batch STRING
        ) USING iceberg
        LOCATION 's3://{args["bucket"]}/warehouse/dim_customer'
        TBLPROPERTIES ('format-version'='2')
    """)

    # Compare against current records only. A replay of the same batch is a no-op.
    current = spark.table(table).filter("is_current = true")
    joined = incoming.alias("s").join(
        current.alias("t"), F.col("s.customer_id") == F.col("t.customer_id"), "left"
    )
    out_of_order = joined.filter(
        (F.col("s.effective_time") < F.col("t.valid_from"))
        | (
            (F.col("s.effective_time") == F.col("t.valid_from"))
            & (F.col("s.attribute_hash") != F.col("t.attribute_hash"))
        )
    )
    require(
        not out_of_order.take(1),
        "SOURCE_DATA_ERROR: out-of-order or conflicting correction",
    )

    # Materialize this comparison before MERGE so both actions describe the same
    # table state. Run one workflow at a time, as described in the execution guide.
    changed = joined.filter(
        F.col("t.customer_id").isNull()
        | (F.col("s.attribute_hash") != F.col("t.attribute_hash"))
    ).select("s.*", F.col("t.customer_id").alias("existing_customer"))
    changed = changed.cache()
    changed_count = changed.count()

    # Every changed existing customer gets an expire row and an insert row. The
    # insert row has a null match key, so it cannot match and close the old version.
    inserts = changed.withColumn("merge_key", F.lit(None).cast("string"))
    expires = changed.filter("existing_customer IS NOT NULL").withColumn(
        "merge_key", F.col("customer_id")
    )
    inserts.unionByName(expires).createOrReplaceTempView("customer_actions")
    if changed_count:
        spark.sql(f"""
            MERGE INTO {table} t USING customer_actions s
            ON t.customer_id = s.merge_key AND t.is_current = true
            WHEN MATCHED THEN UPDATE SET
                t.valid_to = s.effective_time, t.is_current = false
            WHEN NOT MATCHED THEN INSERT (
                customer_version_id, customer_id, city, kyc_status, risk_rating,
                attribute_hash, valid_from, valid_to, is_current, source_batch
            ) VALUES (
                sha2(concat(s.customer_id, '|', cast(s.effective_time AS STRING)), 256),
                s.customer_id, s.city, s.kyc_status, s.risk_rating,
                s.attribute_hash, s.effective_time, NULL, true, '{args["batch_date"]}'
            )
        """)

    # The log records the deployed revision, which helps relate a failure to GitHub.
    print(
        f"CUSTOMER_SUCCESS batch={args['batch_date']} changed={changed_count} commit={args['source_commit']}"
    )
    changed.unpersist()
    incoming.unpersist()


if __name__ == "__main__":
    main()
