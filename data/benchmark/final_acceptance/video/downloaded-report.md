研究问题：根据内部文档，仅核对一个 Claim：EvidencePilot 默认 Chunk Size 和 Overlap 各是多少？
状态：completed；工具调用：2；剩余预算：4。
终止动作：STOP；加权 Coverage：100.0%。
只有显式验证通过且满足来源和时效要求的 Claim 才标为 SUPPORTED；未覆盖部分弃答，不给出确定结论。

C1 [SUPPORTED]：EvidencePilot 默认 Chunk Size 和 Overlap 各是多少？
SUPPORTED: 2 fresh, exact excerpts; source requirements met; semantic verification performed. Both internal sources explicitly confirm the default Chunk Size is 500 tokens and Overlap is 80 tokens.
- Evidence df3c6323-b54b-59c2-8817-0b4980cb276d；关系：context；来源：document:1545a987-664e-4351-92bb-69d047a8afb6#chunk=86029370-6df7-441f-a01f-824df69d529f
> # EvidencePilot 默认参数
> 
> EvidencePilot 的默认 Chunk Size 为 500 tokens，Overlap 为 80 tokens。
> 
> 这份演示资料用于核对内部配置，不涉及敏感信息。
- Evidence 9cd3f71f-edaa-5399-928c-919bae7908c5；关系：context；来源：document:47c456e7-c23f-4f54-bf7a-b56a347c3f36#chunk=95d6a6cc-9e32-4c3f-9e6e-d1a5bb7b50ac
> # EvidencePilot 默认参数
> 
> EvidencePilot 的默认 Chunk Size 为 500 tokens，Overlap 为 80 tokens。
> 
> 这份演示资料用于核对内部配置，不涉及敏感信息。