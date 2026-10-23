# Bedrock Knowledge Base RAG PoC (CloudFormation, Aurora pgvector / S3 Vectors, existing VPC)

Documents uploaded to S3 are automatically indexed into an Amazon Bedrock Knowledge Base. A Lambda in your existing VPC answers questions with a Bedrock model.

The vector store is selected with the `VectorStore` parameter:

| `VectorStore` | Store | Search | Notes |
|---|---|---|---|
| **`PGVECTOR` (default, primary flow)** | Aurora PostgreSQL Serverless v2 + pgvector, **inside your VPC** | **Hybrid** (keyword + semantic) by default | About US$45/month minimum at 0.5 ACU |
| `S3_VECTORS` (kept for testing) | Amazon S3 Vectors | Semantic only | No minimum cost |

```
S3 docs bucket (kb/ prefix, *.metadata.json sidecars)
   │  Object Created / Deleted
   ▼
EventBridge rule ──▶ SQS queue (batching, retries, DLQ)
                        │
                        ▼
  ┌──────────────────── your existing VPC (private subnets) ────────────────────┐
  │  Sync Lambda ──▶ [existing bedrock-agent endpoint] ───────────────────────────┼─StartIngestionJob─▶ Bedrock Knowledge Base
  │                                                                               │                       parse → chunk → embed
  │  Query Lambda ─▶ [existing bedrock-agent-runtime endpoint] ───────────────────┼─RetrieveAndGenerate─▶      │
  │                                                                               │                            │ RDS Data API
  │  Aurora PostgreSQL Serverless v2 (pgvector)  ◀────────────────────────────────┼────────────────────────────┘
  │    bedrock_integration.bedrock_kb  (no inbound SG rules)                      │
  └───────────────────────────────────────────────────────────────────────────────┘
        (S3_VECTORS mode: the KB writes to an S3 Vectors index instead of Aurora)
```

## What gets deployed

| Resource | Purpose |
|---|---|
| `KmsKey` + `alias/<ProjectName>-kb` | Customer-managed KMS key with rotation, used by every resource that supports KMS. See [Encryption](#encryption-kms). |
| `DocsBucket` | Source documents. SSE-KMS with an S3 Bucket Key, versioned, public access blocked, TLS-only, EventBridge notifications on. |
| **pgvector mode** | |
| `AuroraCluster` + `AuroraInstance` | Aurora PostgreSQL Serverless v2 in your private subnets. Storage encrypted with the CMK, Data API enabled, master password managed by RDS in Secrets Manager (CMK), 7-day backups. |
| `DbSubnetGroup`, `DbSecurityGroup` | Uses your existing subnets. The security group has **no inbound rules**, because Bedrock reaches the database through the RDS Data API, not over the network. |
| `BedrockDbSecret` | Credentials for a least-privilege `bedrock_user` role. Bedrock never uses the master user. |
| `PgSchema` (custom resource) + `PgSchemaInitFunction` | Creates the `vector` extension, the `bedrock_integration` schema, `bedrock_user`, the table and the indexes. See [Database schema](#database-schema-pgvector). |
| **S3 Vectors mode** | |
| `VectorBucket` + `VectorIndex` | S3 Vectors store (cosine, 1024 dims, CMK-encrypted). Created only when `VectorStore=S3_VECTORS`. |
| **Common** | |
| `KnowledgeBase` + `KnowledgeBaseRole` | Bedrock KB using Titan Text Embeddings V2. The role only gets permissions for the selected vector store. |
| `DocsDataSource` | S3 data source over `kb/` with fixed-size chunking (400 tokens, 15% overlap) and transient data encrypted with the CMK. |
| `DocsChangedRule` → `SyncQueue` → `SyncFunction` | Auto-sync. Changes are batched for 60s. If a job is already running, the batch is retried after 5 minutes. |
| `QueryFunction` | RAG endpoint (`RetrieveAndGenerate` with citations) or raw `Retrieve`. Defaults to HYBRID search in pgvector mode and SEMANTIC in S3 Vectors mode. |
| `LambdaSecurityGroup` | Attached to the sync and query Lambdas. HTTPS egress only. |

**The template does not create** a VPC, subnets, route tables, or VPC endpoints.

## Database schema (pgvector)

`PgSchemaInitFunction` runs once at deploy time. It runs **outside** the VPC and talks to Aurora through the **RDS Data API**, an HTTPS AWS API authenticated with IAM and the RDS-managed master secret. So it needs no NAT, no VPC endpoint and no database ingress rule. It executes:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS bedrock_integration;
CREATE ROLE bedrock_user LOGIN;  ALTER ROLE bedrock_user PASSWORD '<from BedrockDbSecret>';
GRANT USAGE, CREATE ON SCHEMA bedrock_integration TO bedrock_user;

CREATE TABLE bedrock_integration.bedrock_kb (
  id uuid PRIMARY KEY,
  embedding vector(1024),     -- = EmbeddingDimensions
  chunks text,
  metadata json,
  custom_metadata jsonb
);
CREATE INDEX ... USING hnsw (embedding vector_cosine_ops) WITH (ef_construction = 256);  -- semantic search
CREATE INDEX ... USING gin (to_tsvector('simple', chunks));                              -- enables HYBRID search
CREATE INDEX ... USING gin (custom_metadata);                                            -- metadata filtering
GRANT SELECT, INSERT, UPDATE, DELETE ON bedrock_integration.bedrock_kb TO bedrock_user;
```

- The function retries while the Serverless v2 instance starts up.
- It logs the pgvector version, which must be 0.5 or later for HNSW.
- The password never appears in logs.
- Every statement is idempotent, so a stack update that re-runs it is safe.

**Inspecting the data.** Use the RDS console **Query Editor** with the `AuroraMasterSecretArn` output and database `bedrockkb`, or the Data API:

```bash
aws rds-data execute-statement --resource-arn <AuroraClusterArn> --secret-arn <AuroraMasterSecretArn> \
  --database bedrockkb --sql "SELECT count(*), max(length(chunks)) FROM bedrock_integration.bedrock_kb"
```

## Networking

**Inside your VPC:**
- The sync and query Lambdas, which reach Bedrock through your existing interface endpoints.
- The Aurora cluster (pgvector mode).

**How Bedrock reaches Aurora:** through the RDS Data API. That's why the database security group has no inbound rules. Add ingress later only if applications need direct SQL access on port 5432.

**Outside the VPC:** S3, S3 Vectors, the KB and its ingestion, SQS, EventBridge, and the RDS Data API. These are regional managed services, and the traffic between them stays on the AWS network.

## Encryption (KMS)

One customer-managed key (`KmsKey`, rotation enabled) encrypts everything that supports KMS:

| Resource | How it's encrypted |
|---|---|
| Docs bucket (S3) | Default SSE-KMS with an S3 Bucket Key. The bucket policy **denies** uploads that request SSE-S3 or a different KMS key. |
| Aurora cluster storage, backups and snapshots | `StorageEncrypted` + `KmsKeyId` (pgvector mode) |
| Aurora master secret (RDS-managed) and `BedrockDbSecret` | Secrets Manager with the CMK |
| S3 Vectors bucket + index | `EncryptionConfiguration: aws:kms`, immutable (S3 Vectors mode) |
| KB ingestion (transient data) | Data source `ServerSideEncryptionConfiguration` |
| `RetrieveAndGenerate` sessions | `sessionConfiguration.kmsKeyArn`, passed when a session starts |
| SQS sync queue + DLQ | `KmsMasterKeyId` |
| Lambda environment variables | `KmsKeyArn` on all functions |
| CloudWatch log groups | `KmsKeyId`. Log groups are created before their functions. |

**Key policy grants (least privilege):**

| Principal | Access | Scope |
|---|---|---|
| Account root | Delegates to IAM | Lets IAM policies grant use of the key. |
| `indexing.s3vectors.amazonaws.com` | `kms:Decrypt` | Limited to this account's vector buckets, with encryption context. |
| `bedrock.amazonaws.com` | `GenerateDataKey`, `Decrypt` | Session encryption, limited to this account. |
| `events.amazonaws.com` | `GenerateDataKey`, `Decrypt` | Writing to the encrypted queue, limited to this account. |
| `logs.<region>.amazonaws.com` | Encrypt/decrypt | Only for `/aws/lambda/<ProjectName>-*` log groups. |

The KB role and the Lambda roles get access to this one key through IAM. RDS and Secrets Manager use the key on behalf of the deploying principal and the roles, through the root statement and IAM.

**Who uploads documents:** any IAM principal uploading needs `kms:GenerateDataKey` on the key (`KmsKeyArn` output).

**Key retention:** the key has `DeletionPolicy: Retain` and survives stack deletion. After tear-down, schedule its deletion with `aws kms schedule-key-deletion --key-id <KmsKeyArn> --pending-window-in-days 7`.

## Prerequisites

1. **Region.** Use one where Bedrock Knowledge Bases, your chosen vector store and your models are available.
2. **Model access.** Enable Titan Text Embeddings V2 and the generation model. The default generation model is Claude Sonnet 4.5 via the `us.` inference profile.
3. **Existing VPC.** DNS support and hostnames on, with two or more private subnets in **different AZs**. Aurora's subnet group needs at least two AZs.
4. **Existing Bedrock interface endpoints** (`bedrock-agent`, `bedrock-agent-runtime`) with private DNS. Their security group must allow 443 from the Lambda subnets, for example from your VPC CIDR.
5. **Aurora version.** `AuroraEngineVersion` (default `16.6`) must be available in your region:
   ```bash
   aws rds describe-db-engine-versions --engine aurora-postgresql \
     --query 'DBEngineVersions[?starts_with(EngineVersion,`16`)].EngineVersion'
   ```
6. **IAM.** Permission to create IAM roles (`CAPABILITY_NAMED_IAM`), KMS keys, RDS clusters and Secrets Manager secrets. The deploying principal needs `kms:CreateGrant`, `kms:GenerateDataKey` and `kms:Decrypt` on the new key, because RDS and S3 Vectors use the key on the deployer's behalf. Account admins get this through the key policy's root statement.

## Deploy

```bash
# Primary flow: Aurora pgvector
aws cloudformation deploy \
  --stack-name kb-poc \
  --template-file template.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1 \
  --parameter-overrides \
      VpcId=vpc-0123456789abcdef0 \
      SubnetIds=subnet-0aaa1111bbbb2222c,subnet-0ddd3333eeee4444f
# optional: AuroraEngineVersion=16.x AuroraMinCapacity=0.5 AuroraMaxCapacity=4 GenerationModelId=...

# Alternative: S3 Vectors (deploy as a separate stack to compare side by side)
aws cloudformation deploy --stack-name kb-poc-s3v --template-file template.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ProjectName=kb-poc-s3v VectorStore=S3_VECTORS \
      VpcId=vpc-0123456789abcdef0 SubnetIds=subnet-0aaa1111bbbb2222c,subnet-0ddd3333eeee4444f
```

Deployment takes about **15–20 minutes** in pgvector mode, mostly creating the Aurora cluster and instance, and about 3–5 minutes in S3 Vectors mode.

**Switching modes on an existing stack replaces the Knowledge Base** and re-creates the vector store. Re-run a sync afterwards. To compare both, deploy two stacks with different `ProjectName` values, as shown above.

## Test

`test.sh` runs from your laptop using public AWS APIs, so it works with private Lambdas:

```bash
export AWS_REGION=us-east-1
chmod +x test.sh && ./test.sh kb-poc        # or ./test.sh kb-poc-s3v
```

### Query payloads

```jsonc
// RAG with citations (HYBRID by default in pgvector mode)
{"question": "What is the SEV1 acknowledgement SLA?"}

// Force semantic-only (useful to compare against hybrid in pgvector mode)
{"question": "...", "searchType": "SEMANTIC"}

// Metadata filter (attributes come from <file>.metadata.json -> stored in custom_metadata)
{"question": "...", "filter": {"andAll": [
   {"equals": {"key": "department", "value": "security"}},
   {"greaterThanOrEquals": {"key": "year", "value": 2025}}]}}

// Raw retrieval (no LLM) for debugging relevance
{"question": "...", "mode": "retrieve", "numberOfResults": 5}

// Multi-turn: pass back sessionId from the previous answer
{"question": "and for SEV2?", "sessionId": "<from previous response>"}
```

In S3 Vectors mode, `"searchType": "HYBRID"` is rejected; only SEMANTIC is supported.

## Adding your own documents

```bash
aws s3 cp ./my-docs/ s3://<DocsBucketName>/kb/ --recursive
```

- **Formats:** PDF, DOCX, TXT, MD, HTML, CSV, XLS/XLSX.
- **Metadata sidecar (optional):** `<file>.metadata.json`, for example `{"metadataAttributes": {"department": "security"}}`.
- **Manual sync:** `aws bedrock-agent start-ingestion-job --knowledge-base-id <KbId> --data-source-id <DsId>`

## Cost notes

| Component | Approximate cost |
|---|---|
| Aurora Serverless v2 (pgvector mode) | About US$0.12 per ACU-hour. At 0.5 ACU minimum that's about US$45/month, plus storage and I/O. `AuroraMinCapacity` doesn't offer 0 (auto-pause), because a paused database makes Bedrock calls time out. |
| S3 Vectors (S3 Vectors mode) | Storage, writes and queries only. No minimum. |
| Secrets Manager | About US$0.40/month per secret (two in pgvector mode). |
| KMS key | About US$1/month, plus requests. |
| Bedrock | Embedding tokens at ingestion, plus generation tokens per query. |

## Troubleshooting

| Symptom | Check |
|---|---|
| `AuroraCluster` fails: engine version not available | Pick a supported version with `describe-db-engine-versions` (see prerequisites). |
| `AuroraCluster` fails: subnet group needs 2 AZs | `SubnetIds` must cover at least two Availability Zones. |
| `PgSchema` stuck or fails with "Out of time waiting for Aurora Data API" | The function couldn't reach the database through the Data API for about 14 minutes. Check `/aws/lambda/kb-poc-pg-schema-init` for the repeated error code. Common causes: an SCP or permission boundary blocking `rds-data:*` or `secretsmanager:GetSecretValue`, the Data API not available for the chosen engine version in your region, or the instance not yet available. The custom resource has `ServiceTimeout: 960`, so the stack fails within about 16 minutes instead of hanging for an hour. |
| `PgSchema` fails (other errors) | Logs in `/aws/lambda/kb-poc-pg-schema-init`. Possible causes: an SCP blocking `rds-data:*`, a missing KMS permission on the secrets, or pgvector unavailable on the chosen version. |
| `KnowledgeBase` fails with an RDS or Secrets Manager error | The KB role needs `rds-data:*Statement` on the cluster, `secretsmanager:GetSecretValue` on `BedrockDbSecret` and `kms:Decrypt` on the key. All of these are in the template, so check for SCPs or permission boundaries. |
| Ingestion fails with a permission error on the table | Re-run the stack update; the schema init re-applies grants. Or check the grants with the Query Editor. |
| Sync or query Lambda times out | The Bedrock VPC endpoint isn't reachable. Check its security group allows 443 from the Lambda subnets and that private DNS is on. |
| `ValidationException` for HYBRID | You're in S3 Vectors mode. Use `"searchType": "SEMANTIC"`. |
| Upload fails with `AccessDenied` (KMS) | Your principal needs `kms:GenerateDataKey` on `KmsKeyArn`. Don't pass `--sse AES256`. |

## Clean up

```bash
aws s3 rm s3://<DocsBucketName> --recursive
aws s3api delete-objects --bucket <DocsBucketName> \
  --delete "$(aws s3api list-object-versions --bucket <DocsBucketName> \
  --query '{Objects: [Versions,DeleteMarkers][][].{Key:Key,VersionId:VersionId}}' --output json)"
aws cloudformation delete-stack --stack-name kb-poc
```

What happens on deletion:
- **Aurora:** deleted **without a final snapshot** (`DeletionPolicy: Delete`, PoC setting).
- **Secrets:** Secrets Manager schedules both secrets for deletion with its default recovery window. Their names are auto-generated, so an immediate redeploy doesn't conflict.
- **KMS key:** retained.
- **Duration:** deletion takes about 15–25 minutes.

## PoC simplifications → production next steps

- **Aurora:**
  - `DeletionPolicy: Snapshot` and `DeletionProtection: true`.
  - Multi-AZ (add a second reader instance).
  - Secret rotation for `bedrock_user`.
  - Performance Insights.
- **Key management:** if your organization uses central keys, switch `KmsKey` to an existing key ARN parameter.
- **API front door:** add API Gateway with a private REST API (`execute-api` endpoint) or IAM auth in front of the query Lambda.
- **Access control:** derive `filter` from the caller's identity (for example `tenant_id`), never from request input. Aurora also lets you join retrieval with a permissions table.
- **Guardrails:** add an `AWS::Bedrock::Guardrail` and pass `guardrailConfiguration` in `RetrieveAndGenerate`.
- **Retrieval quality:** reranking (Amazon or Cohere Rerank), hierarchical or semantic chunking, and advanced parsing for scanned PDFs.
- **Audit:**
  - CloudTrail data events.
  - Bedrock model invocation logging.
  - Aurora `pgaudit`.
