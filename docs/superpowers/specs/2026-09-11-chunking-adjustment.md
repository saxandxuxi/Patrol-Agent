# 知识库分块策略调整

> 日期：2026-09-11
> 状态：已实现

## 背景

最初实现采用本地“原子预切分”，把 Markdown 标题和 fenced code block 拆成独立文档上传。实际检查发现，单独上传的代码块会丢失“这个示例是做什么的”语义上下文。

## 调整

SQL/API 类 Markdown 文件不再本地预切分，改为整文件上传，由 RAGFlow 自带 `naive` 解析器处理：

- `chunk_method=naive`
- `chunk_token_num=1024`
- `delimiter=\n`

RAGFlow 的 Markdown 解析器会识别并保护 fenced code range，代码块不会被从中间切断；随后按 token 上限把标题、说明文字和代码块合并进同一个 chunk。

## 同步规则

一个本地文件对应一个 RAGFlow document：

```text
相对路径 + 内容哈希 -> RAGFlow document_id
```

- 本地新增：上传并解析；
- 本地修改：删除旧 document，重新上传并解析；
- 本地删除：删除对应 RAGFlow document；
- 数据集存在时同步更新分块配置。

## 验证

用 `mysql语句知识/select.md` 在 RAGFlow 容器内直接调用其 Markdown 解析器和 `naive_merge` 验证：21 KB 文档生成 5 个 chunk，每个 chunk 都包含完整 SQL 代码块和相邻说明文字。
