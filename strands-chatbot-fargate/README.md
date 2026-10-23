# Strands chatbot on ECS Fargate

A hello-world agentic chat app: a Strands agent (with a weather tool that calls a REST API) behind a FastAPI web app with a simple chat UI, running on ECS Fargate (ARM64) behind an Application Load Balancer in your existing VPC.

```
strands-chatbot-fargate/
├── deploy.ipynb          # run this: build + deploy + test + teardown
├── app/
│   ├── app.py            # FastAPI + Strands agent + tools
│   ├── static/index.html # chat UI
│   ├── requirements.txt
│   └── Dockerfile
└── cfn/
    ├── build.yaml        # ECR repo + CodeBuild project (ARM64 image build)
    └── service.yaml      # ECS cluster, roles, security groups, ALB, Fargate service
```

## How it works

Browser → ALB (port 80) → Fargate task (port 8080) → Strands agent → Bedrock model, plus tool calls to REST APIs.

- `POST /api/chat` with `{"message": "...", "session_id": "..."}` returns `{"reply", "session_id", "tools_used"}`.
- Each `session_id` gets its own agent, so the conversation has memory. Sessions live in the task's memory; ALB stickiness keeps each browser on the same task.

## Deploy

Open `deploy.ipynb` in Jupyter from this folder, fill in the settings cell (bucket, VPC, subnets, model), and run the cells in order. No local Docker is needed; CodeBuild builds the image.

## Run locally

```bash
cd app
pip install -r requirements.txt
uvicorn app:app --port 8080     # uses your local AWS credentials for Bedrock
# open http://localhost:8080
```

## Networking checklist

- **Task subnets** need a route to Bedrock, ECR, CloudWatch Logs and any tool APIs: a NAT Gateway, or VPC endpoints for `bedrock-runtime`, `ecr.api`, `ecr.dkr`, `logs` and an S3 gateway endpoint. Public tool APIs (like the weather API) require NAT.
- **ALB:** `internal` (default) is reachable only from inside the VPC or your corporate network; set `LoadBalancerScheme=internet-facing` with public subnets to expose it. Restrict `AllowedCidr` either way.
- If you use VPC endpoints, their security group must allow inbound 443 from the task security group.

## Adding tools

Add a `@tool` function in `app.py` and append it to `TOOLS`. The docstring and argument descriptions are what the model uses to decide when to call it.

## Production notes

- Add HTTPS: an ACM certificate and a 443 listener on the ALB.
- Add authentication (e.g. ALB + Amazon Cognito, or your SSO).
- To scale past one task without relying on stickiness, store conversation history externally (Strands supports session managers backed by S3, or use AgentCore Memory).
- Narrow the task role's Bedrock `Resource` to the specific model / inference profile ARNs.
