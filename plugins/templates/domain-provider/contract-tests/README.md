# 合同测试要求

领域插件必须在自己的流水线覆盖：

1. discover / collect_evidence / verify 正常返回；
2. 缺依赖、无凭据、超时和上游 5xx 时 fail-closed；
3. 响应字段符合 v1 Resource/Evidence/Verification 合同；
4. 敏感字段被拒绝或脱敏；
5. 未审批动作不能到达执行器；
6. 执行成功但恢复判据失败时任务保持未闭环；
7. 恢复通过时 Trace 能关联 Agent、插件、Skill、动作和验证证据。
