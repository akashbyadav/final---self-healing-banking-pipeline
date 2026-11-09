"""CodeBuild entry point: publish code artifacts and update the parent stack."""

import io
import os
import re
import textwrap
import uuid
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, WaiterError


def stack_outputs(stack):
    """Turn CloudFormation's output list into a readable name-to-value mapping."""
    return {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}


def ensure_idle(outputs):
    """Avoid changing deployed code while a workflow or manually started job runs."""
    if "StateMachineArn" not in outputs:
        return
    states = boto3.client("stepfunctions")
    running = states.list_executions(
        stateMachineArn=outputs["StateMachineArn"], statusFilter="RUNNING", maxResults=1
    )
    if running["executions"]:
        raise RuntimeError(
            "A banking workflow is running. Wait for it to finish, then retry this deployment stage."
        )
    project = outputs["StateMachineArn"].rsplit(":", 1)[-1].removesuffix("-daily")
    glue = boto3.client("glue")
    for suffix in ("customers", "transactions"):
        for page in glue.get_paginator("get_job_runs").paginate(
            JobName=f"{project}-{suffix}"
        ):
            if any(
                run["JobRunState"] in {"STARTING", "RUNNING", "STOPPING", "WAITING"}
                for run in page["JobRuns"]
            ):
                raise RuntimeError(
                    "A Glue job is still active. Retry deployment after it stops."
                )


def upload_zip(client, bucket, key, path, archive_name):
    """Put one Lambda handler at the root of a ZIP, exactly as Lambda expects."""
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.write(path, arcname=archive_name)
    client.put_object(
        Bucket=bucket, Key=key, Body=archive.getvalue(), ContentType="application/zip"
    )


def main():
    """Deploy this CodePipeline revision without reading GitHub credentials."""
    commit = os.environ["SOURCE_COMMIT"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("CodePipeline must supply a full GitHub commit SHA")
    bucket = os.environ["ARTIFACT_BUCKET"]
    stack_name = os.environ["ROOT_STACK_NAME"]
    cfn, s3 = boto3.client("cloudformation"), boto3.client("s3")
    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    ensure_idle(stack_outputs(stack))

    # Every deployed script and template is stored under its Git revision. Old
    # releases stay available until the parent stack is deliberately deleted.
    prefix = f"releases/{commit}"
    for name in ("customer_history.py", "transactions.py"):
        s3.upload_file(f"jobs/{name}", bucket, f"{prefix}/jobs/{name}")
    upload_zip(s3, bucket, f"{prefix}/agent.zip", "agent/handler.py", "handler.py")
    upload_zip(s3, bucket, f"{prefix}/cleanup.zip", "infra/cleanup.py", "cleanup.py")
    application = Path("infra/application.yaml").read_text()
    s3.put_object(
        Bucket=bucket, Key=f"{prefix}/application.yaml", Body=application.encode()
    )

    # Keep the console-deployable parent template synchronized with the same cleanup
    # implementation used by the application stack. No cleanup happens on Update.
    platform = Path("infra/platform.yaml").read_text()
    cleanup = textwrap.indent(Path("infra/cleanup.py").read_text().rstrip(), " " * 10)
    platform = re.sub(
        r"(?<=          # INLINE_CLEANUP_START\n).*?(?=          # INLINE_CLEANUP_END)",
        lambda match: cleanup + "\n",
        platform,
        flags=re.DOTALL,
    )
    s3.put_object(Bucket=bucket, Key=f"{prefix}/platform.yaml", Body=platform.encode())

    # Preserve the settings entered at bootstrap. Only the application template and
    # deployed commit change; each branch continues to own its separate environment.
    parameters = []
    for item in stack["Parameters"]:
        name = item["ParameterKey"]
        if name == "ApplicationTemplateKey":
            parameters.append(
                {"ParameterKey": name, "ParameterValue": f"{prefix}/application.yaml"}
            )
        elif name == "SourceCommit":
            parameters.append({"ParameterKey": name, "ParameterValue": commit})
        else:
            parameters.append({"ParameterKey": name, "UsePreviousValue": True})
    region = boto3.session.Session().region_name
    try:
        cfn.update_stack(
            StackName=stack_name,
            TemplateURL=f"https://{bucket}.s3.{region}.amazonaws.com/{prefix}/platform.yaml",
            Parameters=parameters,
            Capabilities=["CAPABILITY_NAMED_IAM"],
            RoleARN=os.environ["STACK_ROLE_ARN"],
            ClientRequestToken=f"release-{uuid.uuid4()}",
        )
        cfn.get_waiter("stack_update_complete").wait(
            StackName=stack_name, WaiterConfig={"Delay": 15, "MaxAttempts": 120}
        )
    except ClientError as error:
        if "No updates are to be performed" not in str(error):
            raise
    except WaiterError:
        # Events reveal the failed resource without dumping deployment credentials.
        events = cfn.describe_stack_events(StackName=stack_name)["StackEvents"]
        for event in events[:12]:
            print(
                event["LogicalResourceId"],
                event["ResourceStatus"],
                event.get("ResourceStatusReason", ""),
            )
        raise

    # Report where the operator should upload the next batch manually. Deployment
    # publishes code artifacts only; it never generates or uploads source data.
    outputs = stack_outputs(cfn.describe_stacks(StackName=stack_name)["Stacks"][0])
    print(f"Deployed {commit} to {stack_name}.")
    print(f"Manual batch upload destination: s3://{outputs['DataBucketName']}/raw/")
    print(
        "Follow EXECUTION.md to upload each batch, then start its workflow execution."
    )
    print(f"Workflow: {outputs['StateMachineArn']}")


if __name__ == "__main__":
    main()
