# training-checkpoint-shard-compaction-validator
验证检查点分片压缩、引用和提交清单

## 背景

检查点分片经过内容去重后再压缩时，清单可能仍引用旧分片偏移；
新提交也可能只写入了部分压缩块。恢复时若按旧清单映射，可能把
不同参数范围映射到同一数据段。本工具在校验阶段拦截这些问题。

## 输入

纯元数据（不加载真实权重，只校验偏移、摘要与状态）：

- 逻辑分片范围 `LogicalRange(shard, offset, length)`
- 内容块 `ContentBlock(digest, length, references, refcount)`
- 压缩块 `CompressedBlock(block_id, digests, uploaded, bytes_written, bytes_expected)`
- 提交清单 `Manifest(manifest_id, compressed_blocks)`

## 校验规则

- 压缩块必须覆盖所有被引用的逻辑范围，未完整上传的块不算覆盖。
- 同一逻辑范围被映射到不同摘要时判定为重叠冲突（相同摘要的重叠
  属于去重共享，不算冲突）。
- 存在缺失范围、冲突引用或未完整上传的清单不能提交；活动清单
  回滚到最近一个可安全恢复的清单。

## 输出

`ValidationReport`：缺失范围 `missing_ranges`、冲突引用
`conflicts`、可安全恢复的清单 `restorable_manifests` 与当前
活动清单 `active_manifest`。

## 运行测试

```bash
python -m unittest
```

覆盖场景：跨分片共享、压缩中断、重复摘要、清单回滚。
兼容 Python 3.8.2。
