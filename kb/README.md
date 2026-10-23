# Bedrock Knowledge Base RAG PoC (CloudFormation, S3 Vectors, existing VPC)

Documents uploaded to S3 are automatically indexed into an Amazon Bedrock Knowledge Base, with **Amazon S3 Vectors** as the vector store. A Lambda in your existing VPC answers questions with a Bedrock model.

```
S3 docs bucket (kb/ prefix, *.metadata.json sidecars)
   │  Object Created / Deleted
   ▼
EventBridge rule ──▶ SQS queue (batching, retries, DLQ)
                        │
                        ▼
  ┌──────────── your existing VPC (private subnets) ─────────────┐
  │  Sync Lambda ──▶ [existing bedrock-agent endpoint] ──────────┼──StartIngestionJob──▶ Bedrock Knowledge Base
  │                                                               │                       parse → chunk → embed (Titan V2)
  │  Query Lambda ─▶ [existing bedrock-agent-runtime endpoint] ──┼──RetrieveAndGenerate──▶      │
  └───────────────────────────────────────────────────────────────┘                              ▼
                                                                                         S3 Vectors index
```

## What gets deployed

| Resource | Purpose |
|---|---|
| `KmsKey` + `alias/<ProjectName>-kb` | Customer-managed KMS key with automatic rotation, used by every resource below that supports KMS. See [Encryption](#encryption-kms). |
| `DocsBucket` | Source documents. SSE-KMS with an S3 Bucket Key, versioned, public access blocked, TLS-only, EventBridge notifications on. |
| `VectorBucket` + `VectorIndex` | S3 Vectors store (cosine distance, 1024 dimensions). Bedrock requires `AMAZON_BEDROCK_TEXT` and `AMAZON_BEDROCK_METADATA` to be non-filterable. |
| `KnowledgeBase` + `KnowledgeBaseRole` | Bedrock KB using Titan Text Embeddings V2. The role is scoped to the docs prefix, the vector index and the embedding model. |
| `DocsDataSource` | S3 data source over `kb/` with fixed-size chunking (400 tokens, 15% overlap). |
| `DocsChangedRule` → `SyncQueue` → `SyncFunction` | Auto-sync. Changes are batched for 60s. If an ingestion job is already running, the batch is retried after 5 minutes, because only one job per data source can run at a time. |
| `QueryFunction` | RAG endpoint (`RetrieveAndGenerate` with citations) or raw `Retrieve`, with optional metadata filters. |
| `LambdaSecurityGroup` | Attached to both Lambdas. HTTPS egress only. |
| `BedrockEndpointIngressFromLambdas` | Optional. Adds HTTPS ingress from the Lambdas to your existing Bedrock endpoint security group. |

**The template does not create** a VPC, subnets, route tables, or VPC endpoints. It assumes those exist.

## Networking

**Inside your VPC:** the sync and query Lambdas, in the private subnets you pass in. They reach Bedrock through your existing interface endpoints.

**Outside the VPC:** S3, S3 Vectors, the Knowledge Base and its ingestion, SQS and EventBridge. These are regional managed services, and the traffic between them stays on the AWS network.
- The KB reads the docs bucket and reads/writes vectors from Bedrock's own infrastructure, using `KnowledgeBaseRole`.
- Lambda polls SQS and ships logs through the Lambda service. That's why no SQS, Logs or S3 Vectors endpoints are needed.

## Encryption (KMS)

One customer-managed key (`KmsKey`, rotation enabled) encrypts everything that supports KMS:

| Resource | How it's encrypted |
|---|---|
| Docs bucket (S3) | Default SSE-KMS with an S3 Bucket Key. The bucket policy **denies** uploads that request SSE-S3 or a different KMS key. |
| S3 Vectors bucket + index | `EncryptionConfiguration: aws:kms`. Immutable after creation; the index inherits it. |
| KB ingestion (transient data) | `ServerSideEncryptionConfiguration` on the data source encrypts temporary data while documents are parsed, chunked and embedded. |
| `RetrieveAndGenerate` sessions | The query Lambda passes `sessionConfiguration.kmsKeyArn` when it starts a session, so the conversation context that Bedrock stores is encrypted with the key. |
| SQS sync queue + DLQ | `KmsMasterKeyId`. EventBridge is allowed to encrypt messages through the key policy. |
| Lambda environment variables | `KmsKeyArn` on both functions. |
| CloudWatch log groups | `KmsKeyId` on both log groups. The log groups are created before the functions, so logs are never written unencrypted. |

**Key policy grants (least privilege):**

| Principal | Access | Scope |
|---|---|---|
| Account root | Delegates to IAM | Standard. Lets IAM policies grant use of the key. |
| `indexing.s3vectors.amazonaws.com` | `kms:Decrypt` | Background index maintenance, limited to this account's vector buckets with S3 Vectors encryption context. |
| `bedrock.amazonaws.com` | `GenerateDataKey`, `Decrypt` | Session encryption, limited to this account. |
| `events.amazonaws.com` | `GenerateDataKey`, `Decrypt` | Writing to the encrypted queue, limited to this account. |
| `logs.<region>.amazonaws.com` | Encrypt/decrypt | Only for `/aws/lambda/<ProjectName>-*` log groups, via encryption context. |

The KB role and the Lambda roles get `kms:Decrypt` and `kms:GenerateDataKey` on this one key through their IAM policies.

**What can't take a customer-managed key:**
- **EventBridge rule:** events aren't stored; they're encrypted in transit.
- **Bedrock KB control-plane resource:** for a KB with your own vector store, the vector data lives in S3 Vectors, which is encrypted.
- **IAM and security groups:** these hold no data.

**No KMS VPC endpoint is needed.** The Lambdas never call KMS directly. Environment variables are decrypted by the Lambda service, and session encryption is done by Bedrock.

**Who uploads documents:** any IAM principal uploading to the docs bucket needs `kms:GenerateDataKey` on the key. Readers need `kms:Decrypt`. Admins in the account typically already have this. The key ARN is the `KmsKeyArn` stack output.

**Key retention:** the key has `DeletionPolicy: Retain`, so deleting the stack doesn't destroy it. This prevents locking yourself out of any data or backups that are still encrypted with it. After tear-down, schedule deletion manually if you no longer need it:

```bash
aws kms schedule-key-deletion --key-id <KmsKeyArn> --pending-window-in-days 7
```

## Prerequisites

1. **Region.** Use one where Bedrock Knowledge Bases, S3 Vectors and your models are available. `us-east-1` and `us-west-2` are safe defaults. If you need `ca-central-1`, check availability first.
2. **Model access.** Enable Titan Text Embeddings V2 and the generation model in Bedrock. The default generation model is Claude Sonnet 4.5 via the `us.` inference profile; change `GenerationModelId` for other geographies.
3. **Existing VPC.** DNS support and DNS hostnames must be on, with two or more private subnets in different AZs.
4. **Existing Bedrock interface endpoints** with private DNS:
   - `com.amazonaws.<region>.bedrock-agent`, used by the sync Lambda.
   - `com.amazonaws.<region>.bedrock-agent-runtime`, used by the query Lambda.

   Their security group must allow 443 from the Lambdas. Pass that group as `BedrockEndpointSecurityGroupId` and the stack adds the rule, or allow the `LambdaSecurityGroupId` output yourself.
5. **IAM.** AWS CLI v2 with permission to create IAM roles (`CAPABILITY_NAMED_IAM`) and KMS keys. The deploying principal also needs `kms:GenerateDataKey` and `kms:Decrypt` on the new key to create the encrypted vector bucket. Account admins get this through the key policy's root statement.

## Deploy

```bash
aws cloudformation deploy \
  --stack-name kb-poc \
  --template-file template.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1 \
  --parameter-overrides \
      VpcId=vpc-0123456789abcdef0 \
      SubnetIds=subnet-0aaa1111bbbb2222c,subnet-0ddd3333eeee4444f \
      BedrockEndpointSecurityGroupId=sg-0123456789abcdef0
# optional: ProjectName=...  GenerationModelId=<inference-profile-id>  ChunkMaxTokens=500
```

Deployment takes about 3–5 minutes.

## Test

`test.sh` runs from your laptop. It only uses public AWS APIs (S3, Bedrock control plane, `lambda invoke`), so it works even though the Lambdas are in private subnets.

```bash
export AWS_REGION=us-east-1
chmod +x test.sh && ./test.sh kb-poc
```

The script uploads `sample-docs/`, waits for the auto-triggered ingestion job, then asks three questions:
- a plain RAG question
- a RAG question with a metadata filter
- a retrieve-only query

### Query payloads

```jsonc
// RAG with citations
{"question": "What is the SEV1 acknowledgement SLA?"}

// Metadata filter (attributes come from <file>.metadata.json)
{"question": "...", "filter": {"andAll": [
   {"equals": {"key": "department", "value": "security"}},
   {"greaterThanOrEquals": {"key": "year", "value": 2025}}]}}

// Raw retrieval (no LLM) for debugging relevance
{"question": "...", "mode": "retrieve", "numberOfResults": 5}

// Multi-turn: pass back sessionId from the previous answer
{"question": "and for SEV2?", "sessionId": "<from previous response>"}
```

S3 Vectors supports **semantic search only**. Hybrid (keyword + semantic) search needs OpenSearch Serverless as the vector store.

## Adding your own documents

```bash
aws s3 cp ./my-docs/ s3://<DocsBucketName>/kb/ --recursive
```

- **Formats:** PDF, DOCX, TXT, MD, HTML, CSV, XLS/XLSX.
- **Metadata sidecar (optional):** `<file>.metadata.json` next to the file, for example `{"metadataAttributes": {"department": "security"}}`. Keep filterable metadata small, because S3 Vectors limits filterable metadata size per vector.
- **Deletes:** removing a file from S3 removes its vectors on the next sync.
- **Manual sync:** `aws bedrock-agent start-ingestion-job --knowledge-base-id <KbId> --data-source-id <DsId>`

## Cost notes

S3 Vectors has **no minimum charge**. You pay for vector storage, writes and queries, so an idle PoC costs close to nothing. Bedrock charges for embedding tokens at ingestion and generation tokens per query. The template creates no VPC endpoints, so it adds no endpoint charges.

## Troubleshooting

| Symptom | Check |
|---|---|
| Ingestion job fails with a metadata size error | `NonFilterableMetadataKeys` must include `AMAZON_BEDROCK_TEXT` and `AMAZON_BEDROCK_METADATA` (it does). Reduce sidecar metadata, and avoid hierarchical chunking with very large parent chunks. |
| No ingestion job starts | Sync Lambda logs `/aws/lambda/kb-poc-kb-sync`. Confirm the object key starts with `kb/`. |
| Sync or query Lambda times out | The Bedrock VPC endpoint isn't reachable. Check its security group allows 443 from `LambdaSecurityGroupId` and that private DNS is enabled. |
| Ingestion job `FAILED` | `aws bedrock-agent get-ingestion-job ...` → `failureReasons`. |
| `AccessDeniedException` on query | Model access isn't enabled, or the inference profile ID is wrong for the region. |
| Upload fails with `AccessDenied` (KMS) | Your principal needs `kms:GenerateDataKey` on `KmsKeyArn`. Also don't pass `--sse AES256` or a different `--sse-kms-key-id`, because the bucket policy denies those. |
| Ingestion job fails with a KMS error | The KB role is missing permission on the key, or the key is disabled or pending deletion. |
| `ValidationException` about `sessionConfiguration` | The key can only be set when a session starts. The query Lambda sends it only when no `sessionId` is passed. |

## Clean up

```bash
aws s3 rm s3://<DocsBucketName> --recursive
aws s3api delete-objects --bucket <DocsBucketName> \
  --delete "$(aws s3api list-object-versions --bucket <DocsBucketName> \
  --query '{Objects: [Versions,DeleteMarkers][][].{Key:Key,VersionId:VersionId}}' --output json)"
aws cloudformation delete-stack --stack-name kb-poc
```

Deletion can take 10–20 minutes while AWS releases the VPC Lambdas' network interfaces. The KMS key is **retained**; see [Key retention](#encryption-kms) above to schedule its deletion.

## PoC simplifications → production next steps

- **Key management:** if your organization uses centrally managed keys, replace `KmsKey` with an existing key ARN parameter. The key policy statements above must then be added to that key by its owners.
- **API front door:** add API Gateway with a private REST API through an `execute-api` endpoint, or IAM auth, in front of the query Lambda.
- **Access control:** derive `filter` from the caller's identity (for example `tenant_id`), never from request input.
- **Guardrails:** add an `AWS::Bedrock::Guardrail` and pass `guardrailConfiguration` in `RetrieveAndGenerate`.
- **Retrieval quality:**
  - Reranking with Amazon Rerank or Cohere Rerank.
  - Advanced parsing for scanned PDFs.
  - Hierarchical chunking, keeping parent chunks well under 8k tokens with S3 Vectors.
  - OpenSearch Serverless, if you need hybrid search or lower latency.
- **Audit and policies:**
  - CloudTrail data events.
  - Bedrock model invocation logging.
  - Endpoint policies restricting the Bedrock endpoints to this KB.
