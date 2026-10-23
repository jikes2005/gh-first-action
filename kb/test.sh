#!/usr/bin/env bash
# End-to-end test: upload sample docs -> wait for auto-sync -> ask questions.
# Usage: ./test.sh [stack-name]   (uses AWS_REGION / default CLI region)
set -euo pipefail

STACK="${1:-kb-poc}"
out() { aws cloudformation describe-stacks --stack-name "$STACK" \
          --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

BUCKET=$(out DocsBucketName)
PREFIX=$(out DocsPrefix)
KB_ID=$(out KnowledgeBaseId)
DS_ID=$(out DataSourceId)
QUERY_FN=$(out QueryFunctionName)
echo "Bucket=$BUCKET  KB=$KB_ID  DS=$DS_ID"

echo "==> Uploading sample docs to s3://$BUCKET/${PREFIX}"
aws s3 cp "$(dirname "$0")/sample-docs/" "s3://$BUCKET/${PREFIX}" --recursive

echo "==> Waiting for the auto-sync ingestion job (batch window + ingestion)..."
for i in $(seq 1 60); do
  STATUS=$(aws bedrock-agent list-ingestion-jobs --knowledge-base-id "$KB_ID" --data-source-id "$DS_ID" \
            --sort-by attribute=STARTED_AT,order=DESCENDING --max-results 1 \
            --query 'ingestionJobSummaries[0].status' --output text 2>/dev/null || echo NONE)
  echo "   [$i] latest job: $STATUS"
  [[ "$STATUS" == "COMPLETE" ]] && break
  [[ "$STATUS" == "FAILED" ]] && { echo "Ingestion failed"; exit 1; }
  sleep 10
done

aws bedrock-agent list-ingestion-jobs --knowledge-base-id "$KB_ID" --data-source-id "$DS_ID" \
  --sort-by attribute=STARTED_AT,order=DESCENDING --max-results 1 \
  --query 'ingestionJobSummaries[0].statistics'

ask() {
  echo; echo "==> $1"
  aws lambda invoke --function-name "$QUERY_FN" --cli-binary-format raw-in-base64-out \
    --payload "$1" /tmp/kb-answer.json >/dev/null
  python3 -m json.tool /tmp/kb-answer.json
}

ask '{"question": "What is the acknowledgement SLA for a SEV1 incident?"}'
ask '{"question": "How do we contain a compromised EC2 instance?", "filter": {"equals": {"key": "department", "value": "security"}}}'
ask '{"question": "post-mortem deadline", "mode": "retrieve", "numberOfResults": 3}'
