# Bedrock Knowledge Base RAG PoC (CloudFormation, OpenSearch Serverless, existing VPC)

Documents uploaded to S3 are automatically indexed into an Amazon Bedrock Knowledge Base. The vector store is a **private Amazon OpenSearch Serverless (AOSS) collection**. A Lambda in your existing VPC answers questions with a Bedrock model, using hybrid (keyword + semantic) search.

```
S3 docs bucket (kb/ prefix, *.metadata.json sidecars)
   │  Object Created / Deleted
   ▼
EventBridge rule ──▶ SQS queue (batching, retries, DLQ)
                        │
                        ▼
  ┌──────────────── your existing VPC (private subnets) ─────────────────┐
  │  Sync Lambda ──▶ [existing Bedrock VPC endpoint] ──StartIngestionJob──┼──▶ Bedrock Knowledge Base
  │                                                                        │     parse → chunk → embed
  │  Query Lambda ──▶ [existing Bedrock VPC endpoint] ──RetrieveAndGenerate┼──▶      │  (Bedrock service access)
  │                                                                        │          ▼
  │  Index-creator Lambda ──▶ [AOSS VPC endpoint] ─────────────────────────┼──▶ OpenSearch Serverless
  │      (deploy-time only)                                                │    collection (private)
  └────────────────────────────────────────────────────────────────────────┘
```

## What gets deployed

| Resource | Purpose |
|---|---|
| `DocsBucket` | Source documents. Versioned, encrypted, public access blocked, TLS-only, EventBridge notifications on. |
| `VectorCollection` (VECTORSEARCH) | The vector store. |
| Encryption, network and data access policies | Encryption uses an AWS-owned key. The network policy is **private**: the collection is reachable only through the AOSS VPC endpoint and by the Bedrock service (`SourceServices: bedrock.amazonaws.com`). Data access is limited to the KB role and the index-creator role. |
| `AossVpcEndpoint` + security group | AOSS-specific VPC endpoint. It's created only if `ExistingAossVpcEndpointId` is empty. |
| `VectorIndex` (custom resource) | Creates the `bedrock-kb-index` knn index (faiss/HNSW, l2, 1024 dims) before the KB is created. See [How the index is created](#how-the-index-is-created). |
| `KnowledgeBase` + `KnowledgeBaseRole` | Bedrock KB using Titan Text Embeddings V2, writing to AOSS. |
| `DocsDataSource` | S3 data source over `kb/` with fixed-size chunking (400 tokens, 15% overlap). |
| `DocsChangedRule` → `SyncQueue` → `SyncFunction` | Auto-sync. Changes are batched for 60s. If an ingestion job is already running, the batch is retried after 5 minutes. |
| `QueryFunction` | RAG endpoint (`RetrieveAndGenerate` with citations) or raw `Retrieve`. Defaults to **HYBRID** search and supports metadata filters. |
| `LambdaSecurityGroup` | Attached to all in-VPC Lambdas. HTTPS egress only. |

**The template does not create** a VPC, subnets, route tables, or Bedrock VPC endpoints. It assumes those exist.

## Prerequisites

1. **Region.** Use one where Bedrock Knowledge Bases, OpenSearch Serverless and your models are available.
2. **Model access.** Enable Titan Text Embeddings V2 and the generation model in Bedrock. The default generation model is Claude Sonnet 4.5 via the `us.` inference profile; change `GenerationModelId` for other geographies.
3. **Existing VPC.** DNS support and DNS hostnames must be on, with two or more private subnets in different AZs.
4. **Existing Bedrock interface endpoints** with private DNS:
   - `com.amazonaws.<region>.bedrock-agent`, used by the sync Lambda.
   - `com.amazonaws.<region>.bedrock-agent-runtime`, used by the query Lambda.

   Their security group must allow 443 from the Lambdas. Pass that group as `BedrockEndpointSecurityGroupId` and the stack adds the rule, or allow the `LambdaSecurityGroupId` output yourself.
5. **AOSS VPC endpoint.** If the VPC already has an OpenSearch Serverless VPC endpoint, pass it as `ExistingAossVpcEndpointId`. Otherwise one is created. Make sure its security group allows 443 from the `LambdaSecurityGroupId` output.
6. **IAM.** AWS CLI v2 with permission to create IAM roles (`CAPABILITY_NAMED_IAM`) and OpenSearch Serverless resources.

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
# optional: ExistingAossVpcEndpointId=vpce-...  AossStandbyReplicas=ENABLED  GenerationModelId=...
```

Deployment takes about **10–15 minutes**. Collection creation is the slowest step, followed by about 1–3 minutes while the index is created and the data access policy propagates.

## How the index is created

Bedrock requires the vector index to exist before the KB is created. Because the collection is private, the index can only be created from inside the VPC:

1. `IndexProviderFunction` runs **outside** the VPC. It receives the CloudFormation custom-resource request.
2. It synchronously invokes `IndexCreatorFunction`, which runs **inside** the VPC.
3. The creator sends a SigV4-signed `PUT /bedrock-kb-index` through the AOSS VPC endpoint. It retries for about 4 minutes while access policies propagate.
4. The provider waits 60s for the index to become visible, then signals CloudFormation.

The split exists because a Lambda inside the VPC with no NAT can't reach CloudFormation's S3 response URL, and the stack would hang.

**Index mapping:**

| Field | Type |
|---|---|
| `bedrock-knowledge-base-default-vector` | `knn_vector`, faiss HNSW, l2, `EmbeddingDimensions` |
| `AMAZON_BEDROCK_TEXT_CHUNK` | `text`, indexed. Enables hybrid/keyword search. |
| `AMAZON_BEDROCK_METADATA` | `text`, not indexed |

Metadata attributes from sidecar files are added as dynamic fields, which makes them filterable.

## Test

`test.sh` runs from your laptop. It only uses public AWS APIs (S3, Bedrock control plane, `lambda invoke`), so it works even though the Lambdas are private.

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
// RAG with citations (hybrid search by default)
{"question": "What is the SEV1 acknowledgement SLA?"}

// Semantic-only search
{"question": "...", "searchType": "SEMANTIC"}

// Metadata filter (attributes come from <file>.metadata.json)
{"question": "...", "filter": {"andAll": [
   {"equals": {"key": "department", "value": "security"}},
   {"greaterThanOrEquals": {"key": "year", "value": 2025}}]}}

// Raw retrieval (no LLM) for debugging relevance
{"question": "...", "mode": "retrieve", "numberOfResults": 5}

// Multi-turn: pass back sessionId from the previous answer
{"question": "and for SEV2?", "sessionId": "<from previous response>"}
```

## Adding your own documents

```bash
aws s3 cp ./my-docs/ s3://<DocsBucketName>/kb/ --recursive
```

- **Formats:** PDF, DOCX, TXT, MD, HTML, CSV, XLS/XLSX.
- **Metadata sidecar (optional):** `<file>.metadata.json` next to the file, for example `{"metadataAttributes": {"department": "security"}}`.
- **Manual sync:** `aws bedrock-agent start-ingestion-job --knowledge-base-id <KbId> --data-source-id <DsId>`

## Inspecting the collection

The collection is private, so OpenSearch Dashboards is not reachable from the internet. To look at it:
1. Add your IAM principal to the data access policy, either in the AOSS console or in the `Principal` list in `AossDataAccessPolicy`.
2. Query the collection from a host inside the VPC, using a SigV4-signed client such as `awscurl --service aoss`.

## Cost notes

| Component | Approximate cost |
|---|---|
| OpenSearch Serverless | Billed per OCU-hour. With `AossStandbyReplicas=DISABLED` (default), the minimum is about 1 OCU (0.5 indexing + 0.5 search), roughly US$175/month. `ENABLED` (multi-AZ) doubles that minimum. |
| AOSS VPC endpoint | About US$0.01/hr per AZ, if created. |
| Bedrock | Embedding tokens at ingestion, plus generation tokens per query. |

Delete the stack when you're done testing; the OCU minimum is billed even when idle.

## Troubleshooting

| Symptom | Check |
|---|---|
| Stack fails at `VectorIndex` | Logs in `/aws/lambda/kb-poc-aoss-index-creator`. A **timeout or connection error** means the Lambda can't reach the AOSS VPC endpoint: check the subnets, the endpoint's security group (443 from `LambdaSecurityGroupId`), and VPC DNS settings. A **403** that persists means the data access policy or IAM is wrong. |
| Stack fails at `KnowledgeBase` with 401/403 or "no such index" | The network policy must include `bedrock.amazonaws.com` (it does). The data access policy must list the KB role. Retry the deploy, since index visibility can lag. |
| Sync or query Lambda times out | The Bedrock VPC endpoint isn't reachable. Check its security group allows 443 from `LambdaSecurityGroupId` and that private DNS is enabled. |
| Ingestion job `FAILED` | `aws bedrock-agent get-ingestion-job ...` → `failureReasons`. |
| `AccessDeniedException` on query | Model access isn't enabled, or the inference profile ID is wrong for the region. |

## Clean up

```bash
aws s3 rm s3://<DocsBucketName> --recursive
aws s3api delete-objects --bucket <DocsBucketName> \
  --delete "$(aws s3api list-object-versions --bucket <DocsBucketName> \
  --query '{Objects: [Versions,DeleteMarkers][][].{Key:Key,VersionId:VersionId}}' --output json)"
aws cloudformation delete-stack --stack-name kb-poc
```

Deletion can take 15–25 minutes, while VPC Lambda network interfaces are released and the collection is deleted. If you passed `BedrockEndpointSecurityGroupId`, the ingress rule the stack added is removed too.

## PoC simplifications → production next steps

- **Encryption:** use a customer-managed KMS key for the AOSS encryption policy, the docs bucket, SQS and the KB.
- **Availability:** set `AossStandbyReplicas=ENABLED` for multi-AZ.
- **API front door:** add API Gateway with a private REST API through an `execute-api` endpoint, or IAM auth, in front of the query Lambda.
- **Access control:** derive `filter` from the caller's identity (for example `tenant_id`), never from request input.
- **Guardrails:** add an `AWS::Bedrock::Guardrail` and pass `guardrailConfiguration` in `RetrieveAndGenerate`.
- **Retrieval quality:** reranking (Amazon or Cohere Rerank), hierarchical or semantic chunking, and advanced parsing for scanned PDFs.
- **Audit and policies:**
  - CloudTrail data events.
  - Bedrock model invocation logging.
  - Endpoint policies restricting the Bedrock endpoints to this KB.
