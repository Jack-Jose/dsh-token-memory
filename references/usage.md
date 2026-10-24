# 使用方法与文件格式

脚本只读用户指定的输入文件，读写指定记忆根目录，不扫描DSH原始日志、不联网。需要Python 3.9+，无pip依赖。先进入已解压的技能目录，以下相对路径适用于Windows/macOS/Linux；示例ID不是实际会话ID。

```sh
python scripts/memory.py paths
python scripts/memory.py init
python scripts/memory.py --session example-session archive --input examples/record.json
python scripts/memory.py --session example-session checkpoint --input examples/checkpoint.json
python scripts/memory.py --session example-session restore
python scripts/memory.py --session example-session search --query '示例学习'
python scripts/memory.py --session example-session read --path 'dialogue/返回的文件名.txt' --start 1 --lines 25
# 精确读取命中的记录：--record-id 和 --part 取自search结果
# 长单行续读：保留start/lines，以next_offset传入 --offset
# 搜索更早记录：把next_cursor传入 search --query '关键词' --cursor '返回的游标'
```

macOS/Linux通常使用python3；Windows可用py -3或实际python.exe路径。上述example-session只用于演示，真实任务必须替换为真实宿主会话ID。示例同样会写入选择的记忆库，想隔离演示时给每条命令加 `--root ./demo-memory`。

路径选择优先级：`--root` > `DSH_TOKEN_MEMORY_ROOT` 环境变量 > 技能目录 `memory.config.json` 中的root > `$DSH_HOME/token-memory` > `~/.dsh/token-memory`。根目录支持空格、中文、用户主目录展开；配置文件相对root按配置文件所在目录解析。配置损坏会报错，不静默切换空库。`paths`不会创建目录，`init`与首次写入会创建缺失目录。

固定使用同一自定义目录可复制 `memory.config.example.json` 为 `memory.config.json` 后修改root；该私有文件不会进入发布包。更换目录不会自动迁移旧数据，须明确指定原库或自行备份迁移。

输入JSON先用宿主文件工具写入；不是把全文转录通过shell参数打印出来。删除输入暂存文件应遵循用户保留要求，脚本不自动删除。

archive输入：
```json
{
  "kind": "dialogue",
  "key": "message-100-104",
  "title": "明确记忆保存方式",
  "source": "session:真实会话ID;seq:100-104",
  "content": "记录性质：原文摘录/摘要/混合，准确标明。\n用户：……\n助手：……\n覆盖范围：……；缺失范围：……"
}
```

学习记录改为 `kind: learning`，key建议 `bilibili-BV号-r1`，source填写实际URL。记录内容应含标题、作者、获取时间、时间戳证据、结论、待核验项及最终Markdown路径。只有真实可见的原文才标原文。

checkpoint输入：
```json
{
  "covered_through": "message-100-104",
  "summary": "目标：……\n约束/授权：……\n关键决定：……\n已完成：……\n失败/结果未知：……\n下一步：……\n记录/证据路径：……\n未归档范围：……"
}
```

covered_through必须对应本会话已归档的一条记录key；只是调用方确认的覆盖声明，不是脚本验证了宿主全文完整。最多2400字符，超限拒绝而非截断。

## 保存布局

```text
<memory-root>/
  dialogue/对话_YYYYMMDD_会话短ID_v0001.txt
  learning/学习_YYYYMMDD_会话短ID_v0001.txt
  checkpoints/会话哈希/续接_时间_内容哈希.txt
  checkpoints/会话哈希/latest.txt
```

两类正文都是UTF-8 TXT。v2同一会话、同一UTC日期、同类型的增量尽量合卷，达到256 KiB才开下一卷；文件硬上限包含全部元数据。每条逻辑记录以 `DSH-MEMORY ` 后的元数据JSON开头，随后是正文；body_bytes指定精确UTF-8长度，避免正文中类似标记被误认成新记录。原始会话ID、标题、来源和时间在元数据中，文件名用哈希隔离会话。单条超长记录按UTF-8边界分段，保留同一record_id和part/parts编号。

检查点仍最多2400字符；read/search默认2000字符，最多4000，与文件容量分开控制。search每页最多扫描200条正文，元数据仍需本地遍历以筛选会话/排序；这不是常数时间数据库索引。more_records=true用next_cursor继续；避免自动遍历全部分页。

旧版单记录TXT仍可读；超过256 KiB的历史文件不自动覆盖/删除，search会报告skipped_oversized，read会拒绝整份加载。应保留原文件并以新修订key重新分卷。当前更改不批量迁移用户历史。

所有写操作用库根目录排他锁，临时文件原子替换。合卷是保留已有字节后追加新记录，不改写旧记录语义；重复key不追加。检查点保留历史。锁冲突报错，不擅自移除锁；异常退出留下锁时，先确认无正在运行的写操作，再处理 `.write-lock`。

search输出是记录引用和命中附近短片段；read受根路径与会话约束、拒绝路径穿越。跨会话查询必须显式 `--all-sessions`（全局选项，在子命令之前）。恢复只输出短检查点；不会自动写入宿主上下文或标记“模型已读”。

## 启用与限制

把技能完整目录加入当前DSH的技能发现路径。可运行 `python scripts/install.py` 安装到 `$DSH_HOME/skills/dsh-token-memory`（未设置DSH_HOME时为 `~/.dsh/skills/dsh-token-memory`），或用 `--target` 指定完整技能目录。已有目标默认拒绝覆盖；`--upgrade` 会先备份旧的发布文件，再更新，保留私有memory.config.json。目录中存在文件不代表运行中的会话已刷新，需确认技能列表中可见，并在任务开始时采用本技能。其他兼容Agent Skills的宿主可用--target安装，触发行为仍由对应宿主决定。

不修改官方压缩阈值，不替换原生日志，不承诺压缩回调已经接入。源码兼容依据：[官方压缩机制](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-basic/README.md)。
