# Deployment and execution guide

This guide uses GitHub and the AWS consoles. CodePipeline and CodeBuild perform
packaging and deployment in AWS. You do not need a local Python installation, AWS
credentials on your computer, a terminal, or a manually uploaded Lambda ZIP.

The input data is already included in [data/generated/](data/generated/): three
dated customer/transaction batches, one deliberate source-conflict file, and
`expected-results.json`. You can inspect these files in your editor or GitHub.
Upload the required CSVs manually through the S3 console before each iteration,
following the steps below. CodeBuild deploys only code and infrastructure; it never
generates data or uploads source files. No local data-generation step is needed.
Keep the fixed October 28–30, 2026 batch dates when following this guide,
regardless of today's date.

Create the deployment platform once for each environment. After that:

```text
push/merge to dev  → bank-dev-deploy  → update bank-dev application
merge to main     → bank-main-deploy → update bank-main application
```

The platform cannot create itself before it exists; its one-time CloudFormation
upload and GitHub authorization are the bootstrap steps. Subsequent application
and infrastructure changes come from the selected GitHub branch.

## 1. Prepare your account and repository

### AWS account and region

1. Sign in to the AWS account where the resources should be created.
2. Select **US East (N. Virginia), `us-east-1`**, and keep that region throughout
   the guide. The supplied model default is `amazon.nova-pro-v1:0`.
3. Your setup identity must be able to create CloudFormation stacks and the IAM
   roles, S3 buckets, CodeConnections, CodePipeline, CodeBuild, Secrets Manager,
   Lambda, Glue, Step Functions, SNS, CloudWatch Logs, and Athena resources in the
   templates. Creating roles requires acknowledging IAM capabilities. The identity
   used for manual uploads also needs to list the environment's data bucket and
   put objects under its `raw/` prefix.
4. In **Amazon Bedrock**, locate **Amazon Nova Pro** and send a small prompt through
   the text/chat playground. Confirm that a response is returned in this region.
   If your account uses a different model, use a model ID or inference profile that
   supports **Converse tool use**, including tool requests and tool results, as
   `BedrockModelId` below. Plain text-chat support alone is insufficient. The agent
   requests up to 8,000 output tokens per turn so it can propose a complete source
   file; the selected model must support that output length. Nova Pro supports the
   tool interface used here. See [AWS's Nova tool-use documentation](https://docs.aws.amazon.com/nova/latest/userguide/tool-use.html).
5. The templates use normal IAM access to the Glue Data Catalog. If your account
   centrally governs the catalog with Lake Formation, arrange the additional
   project-database permissions with its administrator before running the jobs;
   see the troubleshooting section. Do not change account-wide governance settings
   just to run this project.

AWS charges apply while services run and while data is stored. Glue jobs, CodeBuild,
Bedrock calls, Athena queries, and two separate environments incur usage. Nothing
in this project requires a VPC, NAT gateway, EC2 instance, or database server.

The CloudFormation execution role can provision the services listed in the parent
template, including IAM. It is a deployment role, not the runtime agent's role.
Use an account in which you are authorized to grant those provisioning permissions.

### GitHub repository

Choose either the browser method or the optional Git method below.

#### Browser method: drag folders onto the upload page

1. Create a repository such as `YOUR_OWNER/self-healing-banking-pipeline`. For this
   browser method, initialize it with a README so that `main` exists, and keep
   `main` as the default branch.
2. Open **Add file → Upload files**. The **choose your files** picker can show only
   files; use **drag and drop onto the upload page** to include folders. GitHub
   documents folder drag-and-drop in its
   [browser upload instructions](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository).
3. In Finder or File Explorer, open the local `self-healing-banking-pipeline`
   directory. Drag these items from **inside** it onto the GitHub upload area:

   - Folders: `agent`, `data`, `docs`, `infra`, `jobs`, `scripts`, and `sql`.
   - Files: `.gitignore`, `buildspec.yml`, `requirements.txt`, `README.md`, and
     `EXECUTION.md`.

4. On macOS, press **Command + Shift + .** in Finder to reveal `.gitignore` if it
   is hidden. Upload that file too. Select only the project items listed above.
5. Wait for all uploads. Check paths such as `infra/platform.yaml` and
   `data/generated/2026-10-28/customers.csv`. They must not start with an extra
   `self-healing-banking-pipeline/` directory. The supplied README replaces the
   placeholder created in step 1. Commit the upload to `main`, or merge its PR if
   your repository rules require one.
6. From the branch selector on `main`, create `dev`. Both branches should initially
   contain the complete project.

If your browser does not accept folder drag-and-drop, use the Git method below.
Uploading a ZIP as a repository file will not expand it into the required layout.

#### Optional Git method: publish the complete directory

This alternative requires Git and GitHub authentication on your computer. Start
with a **new, empty GitHub repository**, without a README, license, or gitignore,
and a local project that is not already initialized as a Git repository. Replace
the directory and `YOUR_OWNER` below before running the commands. These commands
are an alternative to the browser upload, not a follow-up to it.

```bash
cd "/path/to/self-healing-banking-pipeline"
git init -b main
git add .
git status
git commit -m "Add banking pipeline project"
git remote add origin https://github.com/YOUR_OWNER/self-healing-banking-pipeline.git
git push -u origin main
git switch -c dev
git push -u origin dev
```

If Git requests your author identity, configure `user.name` and `user.email`, then
retry the commit. Use your configured GitHub credential helper or authentication
prompt for the push. See [GitHub's local repository instructions](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github).

After either method, verify that `buildspec.yml`, `README.md`, and `infra/` appear
directly at the repository root on both branches. Record the repository name as
`YOUR_OWNER/self-healing-banking-pipeline`, without `https://github.com/` or `.git`.
Complete this step before creating the deployment platform. Adding the data files
to GitHub stores them as project inputs; their S3 uploads remain manual.

### GitHub token for repair PRs

The source connection used by CodePipeline and the token used by the repair agent
serve different purposes. Configure both.

1. In GitHub, open **Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token**.
2. Select the resource owner and **Only select repositories**, then select this repo.
3. Give it **Contents: Read and write** and **Pull requests: Read and write**.
   Metadata read access is included by GitHub. No workflow permission is needed.
4. Choose an expiration that covers your planned use. If the organization requires
   token approval, obtain that approval before exercising the repair path.
5. Copy the token into your password manager. You will place it into Secrets Manager
   in step 3. Do not put it in source code, a CloudFormation parameter, or a build log.

## 2. Create the development parent stack

1. Open **CloudFormation → Create stack → With new resources (standard)**.
2. Choose **Upload a template file** and select `infra/platform.yaml` from this folder.
   This is the self-contained parent template, not `infra/application.yaml`.
3. Choose **Next** and enter stack name **`bank-dev`**.
4. Set parameters exactly as follows, replacing the two personal values:

| Parameter | Development value |
| --- | --- |
| `ProjectName` | `bank-dev` |
| `DeploymentBranch` | `dev` |
| `GitHubRepository` | `YOUR_OWNER/self-healing-banking-pipeline` |
| `NotificationEmail` | An email address you can confirm |
| `BedrockModelId` | `amazon.nova-pro-v1:0`, or the model/profile verified in step 1 |
| `ApplicationTemplateKey` | Leave empty |
| `SourceCommit` | Leave empty |

5. Keep the default stack failure behavior so unsuccessful creation rolls back.
   Leave termination protection disabled if you want normal console deletion.
6. Continue to review, acknowledge that CloudFormation creates IAM resources, and
   submit. Wait for **`CREATE_COMPLETE`**.
7. Open the stack's **Outputs** tab. Record `GitHubConnectionArn`, `GitHubSecretArn`,
   and `DeploymentPipelineName`.

At this point the deployment platform exists. The application is deliberately
absent until CodeBuild publishes its first release. A source action can initially
fail because its GitHub connection is still pending; resolve that in the next step.

Do not edit `ProjectName` or rename resources after deployment. It identifies an
environment and is used in its bucket, job, workflow, and database names. To use a
different identity, create a new environment and delete the old one.

## 3. Authorize GitHub and store the repair token

### Complete the source connection

1. Open **Developer Tools → Settings → Connections**. You can also reach Connections
   from the CodePipeline console settings.
2. Find `bank-dev-github`, matching the `GitHubConnectionArn` output.
3. Choose **Update pending connection** or the action to complete its authorization.
4. Sign in to GitHub and install/select the AWS Connector for GitHub App. Grant it
   access to this repository. For organization repositories, an owner may need to
   approve the installation.
5. Finish the connection flow. Its AWS status must become **Available**.

CloudFormation-created connections begin in Pending and require this console
authorization. [AWS connection documentation](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-codeconnections-connection.html)

### Store the repair token

1. Open **Secrets Manager** in the same AWS region.
2. Open the secret whose ARN matches the parent stack's `GitHubSecretArn` output.
3. Choose **Retrieve secret value → Edit**.
4. Replace the empty token with your fine-grained token, keeping this JSON shape:

```json
{"token":"PASTE_YOUR_GITHUB_TOKEN_HERE"}
```

5. Save. No code change or redeployment is needed when this secret is updated.

This secret is unique to the environment. CloudFormation does not create the GitHub
token itself. The agent retrieves the current secret value only if it selects a
repository tool; a notification-only investigation does not need GitHub access.

## 4. Let CodeBuild create the application

1. Open **CodePipeline → bank-dev-deploy**.
2. Choose **Release change** if activation did not already trigger a run. This reads
   the latest `dev` revision. Future pushes to `dev` trigger it automatically.
3. Follow **Source → Deploy**. The Deploy action opens the corresponding CodeBuild
   run; its logs show artifact packaging, the parent-stack update, and the bucket
   destination for your manual batch uploads.
4. Wait for the deployment pipeline to show **Succeeded**. CloudFormation creates a
   nested `Application` stack and finishes the parent update.
5. Return to **CloudFormation → bank-dev → Outputs**. New outputs now include:

| Output | Where it is used |
| --- | --- |
| `DataBucketName` | Raw inputs, Iceberg warehouse, temporary data, and query results |
| `StateMachineArn` | Start and inspect banking batch executions |
| `DatabaseName` | Select the Glue database in Athena; normally `bank_dev` |
| `AthenaWorkGroup` | Select `bank-dev-analytics` in Athena |
| `ApplicationStackId` | Inspect the nested application and its resources |

6. Open the bucket named by **DataBucketName**. On a fresh deployment there are no
   source CSVs in it. Record this bucket name for the manual uploads below. Use the
   **data bucket**, not the separate deployment artifact bucket. Updating an
   existing environment leaves its previously uploaded data in place.

7. Open your email and confirm the SNS subscription from AWS. In SNS, verify that
   the subscription for `bank-dev-incidents` is confirmed, not PendingConfirmation.

The application now exists. Next, manually upload the first batch and start its
workflow. Each upload and execution is a separate operator action.

### Manual upload plan

Local inputs are in this project's `data/generated/` directory. If you only have
the GitHub repository in your browser, choose **Code → Download ZIP**, extract it,
and locate `data/generated/`. Use the actual bucket name from **DataBucketName**
where `<DataBucketName>` appears below.

| When | Local files under `data/generated/` | S3 destination folder | What to do next |
| --- | --- | --- | --- |
| Iteration 1 — step 5 | `2026-10-28/customers.csv` and `2026-10-28/transactions.csv` | `s3://<DataBucketName>/raw/2026-10-28/` | Run October 28 and confirm success. |
| Iteration 2 — step 7 | `2026-10-29/customers.csv` and `2026-10-29/transactions.csv` | `s3://<DataBucketName>/raw/2026-10-29/` | Run October 29 and inspect SCD2 history. |
| Missing-delivery scenario — step 8 | Upload nothing for October 31. | Leave `raw/2026-10-31/customers.csv` and `transactions.csv` absent. | Run the missing-delivery input and inspect SNS. |
| Source-conflict scenario — step 8 | `2026-10-29/customers-conflict.csv` only | `s3://<DataBucketName>/raw/2026-10-29/` | Run the conflict input; keep its distinct filename. |
| Iteration 3 — step 9 | `2026-10-30/customers.csv` and `2026-10-30/transactions.csv` | `s3://<DataBucketName>/raw/2026-10-30/` | Run October 30 with the healthy code or after introducing the documented parser defect. |
| After merging a repair PR — step 9 | No new upload; reuse the October 30 files. | Existing `raw/2026-10-30/` objects | Wait for code deployment, then rerun with a new execution name. |

Keep `expected-results.json` locally for comparing counts and amounts; neither job
reads it. Upload individual CSVs into the stated destination folders. Do not upload
the whole `generated` directory into `raw/`, which would add an unwanted
`generated/` component to the object keys. For the upload controls, see
[AWS's S3 console upload procedure](https://docs.aws.amazon.com/AmazonS3/latest/userguide/upload-objects.html).

## 5. Run the first batch

### Upload iteration 1

1. Open **S3 → Buckets → the bucket from DataBucketName → Objects**.
2. Choose **Create folder**, enter `raw`, and create it. Open `raw/`. If the folder
   already exists, open it directly.
3. Inside `raw/`, create and open a folder named `2026-10-28`.
4. Choose **Upload → Add files**. Select these two local files from this project:
   `data/generated/2026-10-28/customers.csv` and
   `data/generated/2026-10-28/transactions.csv`.
5. Confirm that the destination is `s3://<DataBucketName>/raw/2026-10-28/`, keep
   the existing filenames, and choose **Upload**. Wait for both files to show
   successful upload status.
6. Return to the folder and confirm that the complete object keys are
   `raw/2026-10-28/customers.csv` and `raw/2026-10-28/transactions.csv`.

### Start iteration 1

1. Open **Step Functions → State machines → bank-dev-daily**.
2. Choose **Start execution**. Use a name such as `batch-2026-10-28-initial`.
3. Paste this input, exactly:

```json
{
  "batch_date": "2026-10-28",
  "customers_key": "raw/2026-10-28/customers.csv",
  "transactions_key": "raw/2026-10-28/transactions.csv"
}
```

4. Start the execution and follow `CustomerHistory`, `Transactions`, and `Completed`.
   Glue startup can take several minutes. Wait until the workflow **Succeeded**.
5. If a task fails, select it and inspect its error and Glue job run. Driver logs
   are under `/banking/bank-dev/glue/error` and `/banking/bank-dev/glue/output`.

Run only one workflow at a time. Do not start the next batch or merge deployment
changes until the current workflow finishes. A job's concurrency limit does not
serialize whole workflows for you.

## 6. Query the tables and create the report

1. Open **Athena → Query editor** in `us-east-1`.
2. Select workgroup **`bank-dev-analytics`**, data source **AwsDataCatalog**, and
   database **`bank_dev`**. Use the actual stack outputs if you chose another prefix.
3. The workgroup already supplies the S3 query-results location; do not create a
   separate output bucket.
4. Run these SQL statements individually:

```sql
SELECT COUNT(*) AS customer_versions,
       COUNT_IF(is_current) AS current_customers
FROM dim_customer;
```

Expected: **1,000 customer versions and 1,000 current customers**.

```sql
SELECT COUNT(*) AS unique_transactions FROM fact_transactions;
```

Expected: **3,000**. The input has ten extra identical rows, which are not loaded twice.

5. Open `sql/analytics.sql` in GitHub. Copy the **first statement**, from `CREATE OR
   REPLACE VIEW` through its terminating semicolon, into Athena and run it.
6. Run the file's second `SELECT` statement separately. Athena accepts one statement
   per execution; do not submit both together.
7. Confirm that the report contains the October 28 successful transactions, in INR.

The view is persistent catalog metadata. You do not need to recreate it for every
data batch. If you later change `sql/analytics.sql`, run the updated view statement
again; CodeBuild deploys the runtime but does not execute analytical SQL for you.

## 7. Process customer changes

After iteration 1 succeeds, manually upload iteration 2:

1. In the same data bucket, open `raw/`, then create and open `2026-10-29/`.
2. Choose **Upload → Add files**, and select the local
   `data/generated/2026-10-29/customers.csv` and
   `data/generated/2026-10-29/transactions.csv`.
3. Confirm the destination is `s3://<DataBucketName>/raw/2026-10-29/`, upload,
   and wait for both files to succeed. Leave `customers-conflict.csv` for step 8.
4. Start a new execution named `batch-2026-10-29` with:

```json
{
  "batch_date": "2026-10-29",
  "customers_key": "raw/2026-10-29/customers.csv",
  "transactions_key": "raw/2026-10-29/transactions.csv"
}
```

Wait for success, then inspect one customer's history:

```sql
SELECT customer_id, risk_rating, kyc_status, valid_from, valid_to, is_current
FROM dim_customer
WHERE customer_id = 'C0001'
ORDER BY valid_from;
```

There should be a LOW version from October 28 to October 29 and a current MEDIUM
version starting October 29. The table now has **1,100 versions**, **1,000 current
customers**, and **6,500 transactions**.

October 28 transactions still join to LOW even though that customer's current
rating is MEDIUM. Query `daily_risk_summary` again to inspect both dates.

**For the repair walkthrough, pause here before running October 30.** If you only
want the healthy pipeline, follow the October 30 upload and execution instructions
in step 9 without introducing the regression; the checked-in parser supports it.

## 8. Exercise notification-only failures

### Missing delivery

Do not upload any October 31 files. Confirm that
`raw/2026-10-31/customers.csv` is absent, then start a new execution named
`missing-delivery` with a deliberately absent key:

```json
{
  "batch_date": "2026-10-31",
  "customers_key": "raw/2026-10-31/customers.csv",
  "transactions_key": "raw/2026-10-31/transactions.csv"
}
```

The customer job reports `SOURCE_MISSING`. The workflow routes to `Diagnose`; SNS
should explain that the delivery is missing. No PR should be opened. The execution
ends **Failed**, because an alert does not repair missing source data. The existing
tables are unchanged.

### Conflicting source records

The supplied conflict file contains two different updates for one customer.
Upload it separately to inspect that path:

1. In the data bucket, open the existing `raw/2026-10-29/` folder.
2. Choose **Upload → Add files** and select only
   `data/generated/2026-10-29/customers-conflict.csv`.
3. Keep the name `customers-conflict.csv`; it must not replace `customers.csv`.
   Confirm the destination and wait for the upload to succeed. The transaction
   file from iteration 2 is already present.
4. Start another execution with this input:

```json
{
  "batch_date": "2026-10-29",
  "customers_key": "raw/2026-10-29/customers-conflict.csv",
  "transactions_key": "raw/2026-10-29/transactions.csv"
}
```

The customer job stops before MERGE with `SOURCE_DATA_ERROR`. SNS requests source
correction; the agent must not choose a risk value or remove validation to make the
job succeed. Wait for this execution to finish before proceeding.

## 9. Exercise code repair and complete batch three

This sequence deliberately introduces a small parser regression **in `dev`**. The
repository's normal implementation is correct; you are creating a specific defect
to inspect the diagnosis and repair behavior.

### Introduce the regression through GitHub

1. In GitHub, select the **dev** branch and open `jobs/customer_history.py`.
2. Edit only `parse_effective_time`, replacing that function with:

```python
def parse_effective_time(column):
    """Read timestamps from the customer delivery."""
    # This revision handles only the timezone-free UTC representation.
    return F.to_timestamp(column, "yyyy-MM-dd HH:mm:ss")
```

3. Commit to `dev`, or merge a PR into `dev` if your branch rules require it. Use a
   message such as `Introduce timestamp parser regression for recovery exercise`.
4. Watch **bank-dev-deploy** start automatically and wait for **Succeeded**.
   The main environment, if created, is unaffected.

### Run the supported input that exposes the defect

First, manually upload iteration 3:

- In the data bucket, open `raw/`, then create and open `2026-10-30/`.
- Choose **Upload → Add files**, select the local
  `data/generated/2026-10-30/customers.csv` and
  `data/generated/2026-10-30/transactions.csv`, and confirm the destination is
  `s3://<DataBucketName>/raw/2026-10-30/`.
- Choose **Upload** and wait for both files to succeed. This is also the upload
  procedure when running iteration 3 with the healthy code and no regression.

1. Start `bank-dev-daily` with a new execution name and this input:

```json
{
  "batch_date": "2026-10-30",
  "customers_key": "raw/2026-10-30/customers.csv",
  "transactions_key": "raw/2026-10-30/transactions.csv"
}
```

2. The customer timestamps use `2026-10-30T05:30:00+05:30`, which the source contract
   explicitly allows. The regressed parser returns null, and the job stops with
   `CONTRACT_PARSE_ERROR` before changing customer history.
3. Follow `CustomerIncident → Diagnose`. Lambda runs the agent loop; Bedrock selects
   tools to read the same execution's Glue evidence and the exact deployed source
   revision. It receives each tool result before choosing its next step. A repair
   requires reads of both the failed job and the source contract.
4. Check your SNS email and the GitHub **Pull requests** tab. The repair branch is
   named `repair/<execution-hash>` and the base branch must be **dev**.
5. Review the PR. It should change only `jobs/customer_history.py`, preserve UTC
   normalization and source checks, and support both documented timestamp formats.
   The PR states that syntax was parsed and the proposed code was not executed.
6. Merge the PR after reviewing it. This triggers **bank-dev-deploy** automatically.
   Wait for deployment success before starting another data execution. The code
   deployment leaves your uploaded CSVs unchanged.
7. Start a **new execution with a new name** using the same October 30 input.
   Reuse the existing S3 objects; no new upload is needed. It should finish
   successfully. Do not reuse an old Standard execution name.

The repair handler proposes a change; it does not guarantee a model will produce
the correct patch. If it returns `NEEDS_REVIEW`, inspect the diagnosis and source
contract. You can apply the original two-format parser yourself through a normal
PR. Avoid further commits while diagnosis runs: if the branch changes, the agent
intentionally refuses to patch a stale revision.

### Inspect the agent's tool choices

1. Open **Lambda → Functions → bank-dev-repair → Monitor → View CloudWatch logs**.
   Alternatively, open **CloudWatch → Log groups → /aws/lambda/bank-dev-repair**.
2. Open the latest log stream for this incident and find the entries matching the
   Step Functions execution ARN. `agent_model_turn` shows each model call and its
   stop reason. `agent_tool_result` shows the selected tool, turn number, request
   number, and `success` or `error`. `agent_finished` shows the final outcome and
   counts. Prompts, samples, code, and tokens are not printed by this handler.
3. For the parser defect, expect a sequence similar to the following. The two
   file reads can be requested together, and the model can choose their order:

   ```text
   get_failure_details → read_repository_file (job)
                       → read_repository_file (contract)
                       → create_repair_pr
   ```

   For missing input, the shorter path is usually:

   ```text
   get_failure_details → send_notification
   ```

4. In **Step Functions → execution → Diagnose**, inspect the task output's
   `Payload`. It contains `decision`, `tool_trace`, `model_calls`, and `tool_calls`;
   PR outcomes also contain `pull_request`. A fallback can include
   `agent_stop_reason`. These are executed tool records, not the model's private
   reasoning. The workflow still ends **Failed** after the recovery action.
5. If the agent stops at a limit, inspect the error statuses and SNS explanation.
   Limits are six model calls and eight attempted tool requests per invocation,
   with remaining-time checks inside Lambda's 900-second timeout. Rejected tools
   count toward the limit. A model response without tool requests or with truncated
   output triggers manual review. Do not assume that a longer loop will fix the
   underlying evidence, model-access, or repository issue.

The four tools and their boundaries are defined together in `agent/handler.py`.
There is no Bedrock Agent registration, AgentCore deployment, or Bedrock Flow to
create in the console. The Lambda code sends the tool definitions to Converse on
each model turn. The existing `bedrock:InvokeModel` IAM permission covers Converse;
the SDK and GitHub helpers execute only the allowed tool actions.

### Check the final results

After the three successful batches, the expected counts are:

| Batch | Total customer versions | Current customers | Total unique transactions |
| --- | ---: | ---: | ---: |
| 2026-10-28 | 1,000 | 1,000 | 3,000 |
| 2026-10-29 | 1,100 | 1,000 | 6,500 |
| 2026-10-30 | 1,200 | 1,000 | 10,000 |

Run this independent report comparison:

```sql
SELECT transaction_date,
       SUM(transaction_count) AS successful_transactions,
       SUM(total_amount) AS amount_inr
FROM daily_risk_summary
GROUP BY transaction_date
ORDER BY transaction_date;
```

| Date | Successful transactions | Amount in INR |
| --- | ---: | ---: |
| 2026-10-28 | 2,727 | 6,728,851.15 |
| 2026-10-29 | 3,181 | 7,891,915.59 |
| 2026-10-30 | 3,181 | 7,920,901.77 |

The same values are in the local
[data/generated/expected-results.json](data/generated/expected-results.json).
It does not need to be uploaded to S3. C0001 should have LOW,
MEDIUM, and HIGH versions in order. Rerun the **latest October 30 batch** under a
new execution name; all these totals should stay the same. Do not rerun October 28
customer changes after advancing to October 30: backdated changes are rejected.

## 10. Create and promote to the main environment

1. In GitHub, open a PR from **dev** to **main** and merge the completed, corrected
   implementation. Review the resulting code; the intentional parser regression
   must not be the final state on main.
2. Create a second parent CloudFormation stack by repeating step 2, with these values:

| Setting | Main value |
| --- | --- |
| Stack name | `bank-main` |
| `ProjectName` | `bank-main` |
| `DeploymentBranch` | `main` |
| `GitHubRepository` | The same owner/repository |
| `NotificationEmail` | Your desired main-environment recipient |
| `BedrockModelId` | The same verified model/profile |
| `ApplicationTemplateKey` / `SourceCommit` | Both empty initially |

3. Complete the new `bank-main-github` connection, store the token in the new
   environment's secret, and release the first pipeline change as in steps 3–4.
   You can select the existing GitHub App installation during authorization.
4. Confirm the new SNS email subscription. Its topic is `bank-main-incidents`.
5. Verify that `bank-main-deploy`, `bank-main-daily`, database `bank_main`, workgroup
   `bank-main-analytics`, and a separate data bucket exist.
6. Use **bank-main → Outputs → DataBucketName** to find main's data bucket.
   Repeat the manual uploads from steps 5, 7, and 9 into that bucket, one batch
   at a time. Run October 28, October 29, and October 30 in that order in
   `bank-main-daily`, waiting for each success before uploading the next batch.
   Development's CSVs and tables are not copied to main. Use the healthy code;
   the deliberate failure scenarios are optional and need not be repeated.
7. Create the report view in main's Athena database using `sql/analytics.sql`.

Once both parent stacks exist, pushes to dev deploy only dev. Merging dev into main
deploys main. You do not recreate stacks on each commit. Repair PRs from a main
incident target main; if you merge one there, bring that correction back into dev
through your normal Git workflow.

## 11. Routine updates and rollback

- Change Python, the source contract, or either template in a branch and merge into
  `dev`. CodePipeline deploys the resulting commit automatically.
- Changes to CSVs in GitHub do not upload them to S3. Upload source deliveries
  manually as described above. Code deployments do not regenerate or overwrite
  raw data. The optional `scripts/generate_data.py` utility runs only if you
  deliberately invoke it locally; the supplied files need no generation step.
- Keep deployment changes separate from active data runs. CodeBuild checks for
  running workflows and jobs and stops with a clear message if it finds any. Once
  they finish, retry the failed Deploy action in CodePipeline.
- A push during an active deployment is queued for that environment. Do not start
  data processing until all intended deployments have completed.
- To change stack parameters such as recipient or model, update the **parent** stack
  in CloudFormation using its current template. Keep the existing values of
  `ApplicationTemplateKey` and `SourceCommit`; never blank them after deployment.
- To change a SQL view, run its new CREATE OR REPLACE VIEW statement in each
  environment after the source change is deployed.
- To roll back code, revert the undesired Git commit through a new commit/PR. The
  branch pipeline deploys that new revision. A failed CloudFormation update normally
  rolls infrastructure back to the previous successful definition.
- Infrastructure rollback does not reverse committed Iceberg records. An identical
  current-batch rerun is supported; historical business-data corrections need an
  explicitly designed data operation. Delete/recreate the environment if you want
  to discard all synthetic data and start from a clean state.

## 12. Delete an environment

1. Stop making commits to the environment's branch and stop starting new data runs
   or Athena queries while deletion is in progress.
2. Wait for any CloudFormation create/update already in progress to finish. AWS
   will not accept a conflicting stack operation.
3. Open **CloudFormation** and select the **parent** stack, `bank-dev` or `bank-main`.
   Do not begin by deleting its nested application stack.
4. Choose **Delete**, confirm, and monitor the Events tab. Do not select resource
   retention or force deletion as the normal path.
5. The platform hook stops active deployment work and empties its artifact bucket.
   The application hook stops running workflows, Glue jobs, and Athena queries;
   removes catalog tables and views; aborts multipart uploads; and removes S3 object
   versions and delete markers before the data bucket is removed.
6. CloudFormation removes the remaining application and platform resources. Both
   cleanup Lambdas, their roles, and their declared log groups are removed as well.
7. Wait for **DELETE_COMPLETE**. Deleting dev does not delete main, or vice versa.

Deletion is destructive: raw files, Iceberg data and snapshots, logs, artifacts, and
the environment's Secrets Manager secret are removed. CloudFormation's default
deletion behavior for a secret is force deletion without a recovery window. The
explicit S3 cleanup is what makes a populated environment removable; setting
DeletionPolicy alone would not empty a bucket.

GitHub repositories, branches, PRs, tokens, and the installed GitHub App are outside
CloudFormation. Revoke tokens and uninstall the GitHub App separately if they are
no longer used, after both environments have been deleted. AWS may retain service
execution/build/metric history for its normal retention periods.

If deletion reports **DELETE_FAILED**, inspect the failed cleanup resource and its
CloudWatch log while the stack still exists. Resolve the named permission problem
or wait for an external operation to stop, then choose **Delete** on the parent
again. The hook reports failure rather than falsely marking an incompletely emptied
bucket successful. Very large datasets can exceed the hook's time limit; this
implementation is intended for the supplied small batch workload. Avoid manual
resource replacement, object locks, or external writers in these owned buckets.

## Troubleshooting

| Symptom | What to check and do |
| --- | --- |
| Source action fails on first creation | Finish the Pending connection authorization, verify repository access, then Release change. |
| Pushes do not trigger deployment | Check the configured branch and Available connection; changes to `repair/*` do not trigger either environment. Merge into dev/main. |
| CodeBuild cannot find buildspec | Move this project's contents to the repository root and commit; do not wrap them in another folder. |
| CodeBuild says a workflow/job is active | Wait for it to finish or stop it in the console; then retry the failed deployment stage. |
| Deployment fails in CloudFormation | Read parent and nested stack Events. Use the first resource failure as the cause; subsequent rollback messages are effects. |
| Stack is UPDATE_ROLLBACK_FAILED | Resolve its reported permission/resource issue, use Continue update rollback, and retry deployment only after rollback completes. |
| Glue cannot access catalog resources | Confirm GlueRole IAM permissions and any Lake Formation policy. In a governed account, the administrator must grant the project's Glue role database DESCRIBE/CREATE_TABLE and appropriate SELECT/INSERT/DELETE/ALTER permissions on the project's tables; the Athena user needs database/table query access too. The templates do not alter Lake Formation governance. |
| Glue reports a parser/source error | Inspect the driver's log, input sample, and source contract. Do not remove validation to hide malformed source data. |
| Athena shows no tables | Wait for the first successful batch and select the correct region, workgroup, data source, and database. |
| Input CSVs are missing from S3 | Upload the current batch manually from `data/generated/` into the bucket from `DataBucketName`. Confirm exact `raw/<date>/<filename>.csv` keys and successful upload status. Deployment does not supply source data. |
| Athena rejects multiple statements | Select and execute one SQL statement at a time. |
| SNS email never arrives | Confirm the subscription, check the selected environment's topic, and inspect spam filtering. |
| Repair agent reports missing token or HTTP 401/403 | Update the environment's secret; verify token expiry, owner approval, repo selection, Contents write, and Pull requests write. |
| Bedrock AccessDenied or model-not-found | Verify the exact model/profile in this region and account, model access, and any organization policies; update the parent parameter if needed. |
| Bedrock rejects `toolConfig` or returns only text | Select a model/profile supporting Converse tool use and 8,000 output tokens. Inspect the SNS stop reason and `agent_model_turn` logs. |
| Agent reaches a call limit or remaining-time limit | Inspect `agent_tool_result` for repeated rejected requests or unavailable services. Correct the underlying issue and start a new batch execution when ready; the fallback requests manual review. |
| Agent says the revision changed | A newer branch commit arrived before/during diagnosis. Wait for deployment, rerun the failed batch, and keep that branch stable while it is diagnosed. |
| No PR despite a failed job | Missing/source/permission failures should notify only. For a code defect, inspect the tool trace; source and contract must both be read, and failed/deployed/branch revisions must agree. Missing or malformed evidence requests review. |
| No matching Glue run | The failure tool searches at most the latest 250 runs for the execution ID. A job that never started or an older incident needs manual review. |
| A repair PR exists but execution is Failed | Expected. Merge the reviewed change, wait for deployment, and start a new data execution. |
| Older batch rerun rejected | Process customer changes in order; only rerun the current batch before advancing. Recreate the environment to repeat the full sequence from scratch. |
| S3 bucket will not delete | Inspect the cleanup custom-resource error, active writers, permissions, object versions, and multipart uploads; retry parent deletion after fixing the cause. |

## Validation boundary

The supplied project has local Python syntax/format checks, CloudFormation schema
validation, and deterministic sample-data comparisons. The agent loop has also
been exercised locally using in-memory service responses for notification, PR,
rejected requests, stale revisions, and budget limits. These checks do not invoke
a live model or establish the correctness of its generated repairs. It has not been deployed
to your AWS account as part of preparing these files. Your account's IAM policies,
model access, GitHub authorization, and actual Glue runs are verified by following
the sequence above. No unit-test files or agent evaluation framework are included.
