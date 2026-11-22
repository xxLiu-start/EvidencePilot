# DeepSeek Tool Calls

- **Resource ID:** 07
- **Publisher:** DeepSeek API Docs
- **Official source:** https://api-docs.deepseek.com/guides/tool_calls/
- **Purpose:** Evaluate function calling, strict JSON Schema mode and agent integration.

## Reading notes

The guide describes tool-call request formats, thinking/non-thinking behavior and strict-mode conditions. Enterprise evaluation should test argument accuracy, recovery, idempotency, authorization and auditability, not just whether a tool call can be emitted.

## Enterprise selection questions

- Which tools and schemas are required
- Is strict schema adherence needed
- How are permissions, parameter validation and approvals enforced
- How are retries and duplicate calls handled?
