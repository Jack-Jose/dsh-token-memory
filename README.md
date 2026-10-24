# dsh-token-memory

面向 DeepSeek Harness 长期对话与自主学习的轻量文件记忆 Skill。

**保存更多，读回更少。** 对话与学习分两类UTF-8 TXT，按会话/UTC日期合卷，每卷不超过256 KiB。压缩前由Agent写短检查点，压缩后读回摘要，需要细节时再限量检索原文。

## 能力与边界

- 本地Python标准库；无需API密钥、向量数据库、pip依赖或常驻服务。
- 重试去重、原子替换、写锁、会话隔离、中文/表情安全分卷。
- 默认只读2000字符，最多4000；检查点最多2400字符；正文检索每页最多200条，支持游标。
- 不抓取原始宿主日志，不自动归档未见过的历史，不把别人消息当成授权。
- **不是原生DSH插件，没有自动压缩前/后事件钩子。** Agent需要采用技能并执行保存/恢复；不能保证突发压缩前必定保存。
- 不自动调用模型总结。摘要、工具调用和读取仍消耗宿主token；没有实测数据时不宣称节省百分比。
- 词面检索，无语义向量索引；元数据排序仍会遍历本地文件。长期大库不保证恒定查询时间。
- 明文存储；常见密钥脱敏不等于完整隐私防护，不应归档Cookie/令牌或敏感个人信息。

## 兼容与安装

需要Python **3.9+** 和支持Agent Skills、具备文件/命令工具的宿主。路径使用pathlib设计，Windows/macOS/Linux通用；本地实测Windows，其他平台通过仓库CI进一步验证。没有依赖特定DSH版本的内部API。

下载仓库或ZIP后进入技能目录：

```sh
python scripts/install.py
```

macOS/Linux可用python3，Windows可用py -3。默认安装到 `$DSH_HOME/skills/dsh-token-memory`；未设置DSH_HOME则为 `~/.dsh/skills/dsh-token-memory`。自定义位置用 `--target /your/skills/dsh-token-memory`。升级用 `--upgrade`，保留本机私有配置并先备份发布文件。刷新宿主技能列表，确认技能可见；安装脚本不会自动更改宿主或其他技能。

不需要事先创建特定目录结构：首次写入按需建目录，也可以先运行：

```sh
python scripts/memory.py paths
python scripts/memory.py init
```

记忆库优先级：`--root` > `DSH_TOKEN_MEMORY_ROOT` > 技能目录 `memory.config.json` > `$DSH_HOME/token-memory` > `~/.dsh/token-memory`。私有配置可从 `memory.config.example.json` 复制；不会随仓库发布。路径可含中文和空格，不要求D盘。

## 使用

让DSH采用本技能，使用真实会话ID归档新增对话或学习成果；阶段结束/接近压缩时写检查点。压缩后恢复一次摘要，再按需search/read，禁止整库读回。具体命令、输入格式和演示见 [使用说明](references/usage.md)。

演示请隔离数据：

```sh
python scripts/memory.py --root ./demo-memory --session demo archive --input examples/record.json
python scripts/memory.py --root ./demo-memory --session demo checkpoint --input examples/checkpoint.json
python scripts/memory.py --root ./demo-memory --session demo restore
```

## 验证

```sh
python -B -m unittest discover -s scripts -p test_memory.py -v
```

CI覆盖Windows、Linux、macOS及Python 3.9/3.12。CI配置存在不等于所有平台已通过；请查看实际运行结果。发布包只包含明确列出的技能文件，排除记忆数据、配置、凭据、缓存和备份。

## 类似项目与定位

- [filesystem-context](https://github.com/muratcankoylan/Agent-Skills-for-Context-Engineering/tree/main/skills/filesystem-context)：文件上下文和按需发现的方法论技能。
- [dsh-memento](https://github.com/PerryLink/dsh-memento)：有容量预算与写入审批的原生DSH记忆插件。
- [dsh-memory-evolve](https://github.com/csyangwen/dsh-memory-evolve)：更广泛的跨会话记忆和后台维护插件。

本项目是独立实现，侧重长期对话/自主学习的两类TXT、日合卷、有限读回；不复制上述项目的代码，也不声称替代其原生钩子能力。

## English

A lightweight, offline Agent Skill for long-running DeepSeek Harness conversations and self-directed learning. Separate dialogue/learning TXT volumes (256 KiB maximum), bounded checkpoints, selective retrieval and pagination. Python 3.9+ standard library only; configurable storage with automatic directory creation. No automatic compaction lifecycle hooks, extra model calls or guaranteed token-savings percentage. Run `python scripts/install.py` and check [usage](references/usage.md).
