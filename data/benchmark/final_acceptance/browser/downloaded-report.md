研究问题：根据内部文档，仅核对一个 Claim：EvidencePilot 默认 Chunk Size 和 Overlap 各是多少？
状态：completed；工具调用：2；剩余预算：4。
终止动作：STOP；加权 Coverage：100.0%。
只有显式验证通过且满足来源和时效要求的 Claim 才标为 SUPPORTED；未覆盖部分弃答，不给出确定结论。

C1 [SUPPORTED]：EvidencePilot 默认 Chunk Size 和 Overlap 各是多少？
SUPPORTED: 1 fresh, exact excerpts; source requirements met; semantic verification performed. The provided internal document explicitly confirms the default Chunk Size (500 tokens) and Overlap (80 tokens) for EvidencePilot.
- Evidence 9c9ca6c3-a17a-576e-b760-3b814f677714；关系：context；来源：document:75d678a8-aa5b-4d25-8fbc-1076068561bd#chunk=d6eed2ad-1222-4941-8fc8-4602f982f81c
> # EvidencePilot 默认参数
> 
> EvidencePilot 的默认 Chunk Size 为 500 tokens，Overlap 为 80 tokens。
> 
> 这份演示资料用于核对内部配置，不涉及敏感信息。