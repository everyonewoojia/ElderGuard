---
description: 提交代码（自动同步任务进度到AGENTS.md）
agent: build
---

请按以下步骤执行提交：

1. **同步任务进度到AGENTS.md**
   - 读取当前工作目录下的 AGENTS.md 文件
   - 根据本次提交的内容，更新 AGENTS.md 中的任务进度
   - 如果有新增任务或完成的任务，相应更新状态

2. **执行Git提交**
   - 运行 `git add .`
   - 运行 `git commit -m "$ARGUMENTS"`

3. **确认提交成功**
   - 显示 `git log --oneline -3` 确认提交

注意：如果用户没有提供提交信息（$ARGUMENTS为空），请询问用户提交信息。
