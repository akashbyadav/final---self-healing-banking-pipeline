"""CloudFormation deletion hook: stop writers and remove only stack-owned data."""

import json
import time
from urllib.request import Request, urlopen

import boto3
from botocore.exceptions import ClientError


def remaining(context):
    """Leave enough time to report a failure instead of timing out silently."""
    if context.get_remaining_time_in_millis() < 45000:
        raise TimeoutError(
            "Cleanup needs another attempt. Retry Delete on the parent stack."
        )


def optional_call(client, operation, **arguments):
    """Allow deletion to resume when an earlier attempt already removed a resource."""
    try:
        return getattr(client, operation)(**arguments)
    except ClientError as error:
        code = error.response["Error"]["Code"]
        if code in {
            "EntityNotFoundException",
            "ResourceNotFoundException",
            "PipelineNotFoundException",
            "StateMachineDoesNotExist",
            "NoSuchBucket",
            "404",
        }:
            return {}
        raise


def stop_deployments(properties, context):
    """Stop active pipeline executions and wait for their CodeBuild processes."""
    if properties.get("PipelineName"):
        pipeline = boto3.client("codepipeline")
        # Incoming transitions are disabled before active executions are stopped.
        optional_call(
            pipeline,
            "disable_stage_transition",
            pipelineName=properties["PipelineName"],
            stageName="Deploy",
            transitionType="Inbound",
            reason="CloudFormation stack deletion",
        )
        for page in pipeline.get_paginator("list_pipeline_executions").paginate(
            pipelineName=properties["PipelineName"]
        ):
            for execution in page["pipelineExecutionSummaries"]:
                if execution["status"] in {"InProgress", "Stopping"}:
                    try:
                        pipeline.stop_pipeline_execution(
                            pipelineName=properties["PipelineName"],
                            pipelineExecutionId=execution["pipelineExecutionId"],
                            abandon=True,
                            reason="Stack deletion",
                        )
                    except ClientError as error:
                        if error.response["Error"]["Code"] not in {
                            "PipelineExecutionNotStoppableException",
                            "DuplicatedStopRequestException",
                        }:
                            raise

    if properties.get("BuildProject"):
        build = boto3.client("codebuild")
        ids = []
        for page in build.get_paginator("list_builds_for_project").paginate(
            projectName=properties["BuildProject"]
        ):
            ids.extend(page["ids"])
        for offset in range(0, len(ids), 100):
            for item in build.batch_get_builds(ids=ids[offset : offset + 100])[
                "builds"
            ]:
                if item["buildStatus"] == "IN_PROGRESS":
                    build.stop_build(id=item["id"])
                    while (
                        build.batch_get_builds(ids=[item["id"]])["builds"][0][
                            "buildStatus"
                        ]
                        == "IN_PROGRESS"
                    ):
                        remaining(context)
                        time.sleep(3)


def stop_data_jobs(properties, context):
    """Stop workflow executions, Glue jobs, and Athena queries before emptying S3."""
    if properties.get("StateMachineArn"):
        states = boto3.client("stepfunctions")
        for page in states.get_paginator("list_executions").paginate(
            stateMachineArn=properties["StateMachineArn"], statusFilter="RUNNING"
        ):
            for execution in page["executions"]:
                states.stop_execution(
                    executionArn=execution["executionArn"], cause="Stack deletion"
                )

    glue = boto3.client("glue")
    for name in properties.get("Jobs", []):
        active = []
        for page in glue.get_paginator("get_job_runs").paginate(JobName=name):
            active.extend(
                run["Id"]
                for run in page["JobRuns"]
                if run["JobRunState"] in {"STARTING", "RUNNING", "STOPPING", "WAITING"}
            )
        for offset in range(0, len(active), 25):
            glue.batch_stop_job_run(
                JobName=name, JobRunIds=active[offset : offset + 25]
            )
        for run_id in active:
            while glue.get_job_run(JobName=name, RunId=run_id)["JobRun"][
                "JobRunState"
            ] in {"STARTING", "RUNNING", "STOPPING", "WAITING"}:
                remaining(context)
                time.sleep(3)

    if properties.get("WorkGroup"):
        athena = boto3.client("athena")
        for page in athena.get_paginator("list_query_executions").paginate(
            WorkGroup=properties["WorkGroup"]
        ):
            for query_id in page["QueryExecutionIds"]:
                state = athena.get_query_execution(QueryExecutionId=query_id)[
                    "QueryExecution"
                ]["Status"]["State"]
                if state in {"QUEUED", "RUNNING"}:
                    athena.stop_query_execution(QueryExecutionId=query_id)
                    while athena.get_query_execution(QueryExecutionId=query_id)[
                        "QueryExecution"
                    ]["Status"]["State"] in {"QUEUED", "RUNNING"}:
                        remaining(context)
                        time.sleep(2)


def remove_tables(database):
    """Remove job-created Iceberg tables and Athena views from this database only."""
    glue = boto3.client("glue")
    names = []
    for page in glue.get_paginator("get_tables").paginate(DatabaseName=database):
        names.extend(table["Name"] for table in page["TableList"])
    for offset in range(0, len(names), 100):
        response = glue.batch_delete_table(
            DatabaseName=database, TablesToDelete=names[offset : offset + 100]
        )
        if response.get("Errors"):
            raise RuntimeError("Some catalog tables could not be deleted")


def empty_bucket(bucket, context):
    """Delete all object versions, delete markers, and unfinished multipart uploads."""
    s3 = boto3.client("s3")
    for page in s3.get_paginator("list_multipart_uploads").paginate(Bucket=bucket):
        for upload in page.get("Uploads", []):
            s3.abort_multipart_upload(
                Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"]
            )

    # A versioned bucket is not empty when a normal delete merely adds a marker.
    # Delete each version and marker explicitly, checking per-object errors.
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
        remaining(context)
        items = page.get("Versions", []) + page.get("DeleteMarkers", [])
        for offset in range(0, len(items), 1000):
            response = s3.delete_objects(
                Bucket=bucket,
                Delete={
                    "Objects": [
                        {"Key": item["Key"], "VersionId": item["VersionId"]}
                        for item in items[offset : offset + 1000]
                    ],
                    "Quiet": True,
                },
            )
            if response.get("Errors"):
                raise RuntimeError(
                    "S3 rejected deletion of one or more object versions"
                )

    # Also cover buckets whose versioning was never enabled or was suspended.
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        remaining(context)
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if objects:
            response = s3.delete_objects(
                Bucket=bucket, Delete={"Objects": objects, "Quiet": True}
            )
            if response.get("Errors"):
                raise RuntimeError("S3 rejected deletion of one or more objects")


def handler(event, context):
    """Acknowledge every CloudFormation request, including cleanup failures."""
    properties = event["ResourceProperties"]
    status, reason = "SUCCESS", "Cleanup completed"
    try:
        # Create and Update do not clear data. Cleanup runs only for Delete requests.
        if event["RequestType"] == "Delete":
            stop_deployments(properties, context)
            stop_data_jobs(properties, context)
            if properties.get("Database"):
                remove_tables(properties["Database"])
            empty_bucket(properties["Bucket"], context)
    except Exception as error:
        status, reason = "FAILED", f"{type(error).__name__}: {error}"
        print(reason)

    # The response URL is supplied by CloudFormation. Do not log it: it is signed.
    body = json.dumps(
        {
            "Status": status,
            "Reason": reason[:1000],
            "PhysicalResourceId": event.get(
                "PhysicalResourceId", f"cleanup-{properties['Bucket']}"
            ),
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "Data": {},
        }
    ).encode()
    request = Request(
        event["ResponseURL"],
        data=body,
        method="PUT",
        headers={"Content-Type": "", "Content-Length": str(len(body))},
    )
    with urlopen(request, timeout=20) as response:
        response.read()
