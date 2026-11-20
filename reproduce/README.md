# 课程复现：任务变异与蜕变测试

这份独立实现使用 NumPy、noise、Annoy 和仓库中已编译的 PIBT2 / LaCAM3 求解器。它能做随机基线实验，也能从求解轨迹出发生成变异任务。当前已覆盖论文 Approach 中四类无引导与四类引导智能体变异；其他实验模块仍在逐步完成。

## 运行

从项目根目录运行随机基线：

```bash
python3 reproduce/run_pairs.py --solver pibt \
  --map data/maps/Berlin_0_256.map --size 128 \
  --agents 50 --cases 10 --seed 1 --mode baseline \
  --output reproduce/output/pibt-baseline
```

运行变异版：

```bash
python3 reproduce/run_pairs.py --solver pibt \
  --map data/maps/Berlin_0_256.map --size 128 \
  --agents 50 --cases 10 --seed 1 --mode mutate \
  --output reproduce/output/pibt-mutate
```

`--solver` 可选 `pibt`、`hca`、`pibt_plus`、`lacam3`。`--size` 将**整张**地图按最近邻方式缩放成指定大小；不传则保留原图。初始任务从一个足够大的连通区域抽取互不重复的起终点。`--cases` 指基础任务组数；原任务求解成功时，每组运行原任务及 7 个蜕变任务。上面的 10 组仅用于演示，数量不足以比较论文中的失败率。

小规模预检可用 `--size 32 --agents 20 --patch-size 8`。论文配置采用 128×128、100 个智能体；作者公开代码的地图 patch 默认 32×32，因此正式试验可保留 `--patch-size 32`。

## 本机与服务器入口

本机入口 `python3 reproduce/run_local.py --solver all --cases 3` 固定 128×128、50 agents、单 worker，使用精确最近邻，输出到 `reproduce/output/local/<时间戳>/`。

服务器入口先打印计划，不启动实验：

```bash
python3 reproduce/run_server.py --solver all
```

服务器模式需要先安装 `python3 -m pip install -r reproduce/requirements-server.txt`，否则 `--execute` 会在创建结果目录前报错。服务器按作者代码从 `data/maps/` 每张地图最多生成 3 个初始任务，使用每图固定随机种子 42，并按连通区域大小分配任务。30 张地图最多产生 90 个初始任务；作者设置中的 100 是总上限，不会额外补足。主进程维护共享种子池，32 个工作进程并发处理任务组。每个任务组包含原任务和最多 7 个 MR。每求解器重复 5 次；PIBT、HCA、PIBT+ 各次最多 20,000 组或 1 小时，LaCAM* 最多 25,000 组或 6 小时，先到为止。初始任务结束后开始计时；LaCAM* 每次优化 3 秒，单次求解器调用外层超时默认 40 秒。3 秒与 25% 的阈值取自论文；作者代码 `settings.py` 默认分别为 1 秒与 30%。计划中的输出目录在 `reproduce/output/server/<时间戳>/` 下，与本机结果分开。

服务器保留每组的 `results.jsonl` 统计行和 `manifest.json`；CF、OF、执行异常案例，以及编号每 500 组抽到的一组正常案例，其已写出的地图、输入与求解器结果文件会保留。整组均未解的案例目前不算已证实的 CF/OF，也不会被保留。没有被选中保留的任务目录会在该组统计行写入后删除。`case_files_selected_for_retention` 表示保留策略是否选中该组；如果 worker 在写输入前就出错，目录仍可能不存在。本机入口仍保留全部已写出的文件。

服务器小批量检查可用 `--solver pibt --repeats 1 --workers 2 --init-cases 2 --cases 4 --time-limit 60 --execute`。`--execute` 会实际运行；省略时只显示计划。服务器每次派发和结算任务组都写入 `checkpoint.sqlite3`；中断后对同一时间戳目录执行：

```bash
python3 reproduce/run_server.py \
  --resume-run reproduce/output/server/<原时间戳目录> --execute
```

恢复入口读取原 `server_manifest.json`，让每个已有检查点的批次由核心入口核验并快速返回或继续运行；未开始的批次按原参数启动。已完成任务组的种子池、随机状态、特征索引数据和统计会恢复；若工作进程已写出完整 `payload.json`，该组不会重跑求解器。中断时仍在运行、尚未写出完整缓存的少数任务组需要重试。同一任务目录有运行锁；若旧 worker 尚未退出，恢复会报错，稍后重试即可。停机时间不计入 1h/6h 的 fuzzing 时限；运行时约每 5 秒更新已用时，强制中断仍可能有少量计时误差。检查点只应在可信的原运行目录加载；恢复时会核对求解器参数以及代码、地图和求解器二进制的 SHA-256，防止混用不同配置。真实 Annoy 的服务器规模性能尚需实测，**正式长跑暂不可视为就绪**。

RQ3 使用独立入口和输出目录，按论文在初始化后每个求解器单进程运行 1 小时；仍使用 128×128 地图、100 agents 和 Annoy。默认 `--cases 1000000` 只是防止异常无限循环，正常由时间上限停止。省略 `--execute` 可先查看计划：

```bash
python3 reproduce/run_rq3.py --solver all --execute
python3 reproduce/run_rq3.py \
  --resume-run reproduce/output/rq3/<原时间戳目录> --execute
```

每条 `results.jsonl` 含 `completed_at_utc`、`fuzz_elapsed_seconds`、`phase_seconds` 和 `timing_complete`；`summary.json` 记录最终 `fuzz_elapsed_seconds` 及单进程限时运行的 `rq3_timing`。初始池任务的 `fuzz_elapsed_seconds` 为 `null`；其余任务从初始池完成时起计，恢复时不计停机时间。`phase_seconds` 的四个互斥论文类别是：`mutation`（选种与变异）、`metamorphic`（轨迹热点与 MR 生成）、`execution`（求解器输入准备、求解器运行、结果解析与清理）、`result_analysis`（轨迹分析、特征、评分和新颖度）。诊断子项 `solver`（纯子进程）和 `result_parse` 已包含在 `execution` 内，不能再与它相加；额外保存可重放输入的 `artifact_io` 和 worker 剩余 `other` 另列。`worker_elapsed_seconds` 是 worker 写检查点缓存前的总时长。

`rq3_timing.category_percent` 排除初始池和计时缺失的任务，以四个论文类别之和作为分母，便于与论文图 6 的四段占比对照；`seconds` 保留各类原始秒数及诊断子项。`wall_seconds` 和 `wall_unattributed_seconds` 另示实际单进程墙钟时间与未计入四类的时间，后者混合检查点写盘、调度等开销，**不是精确的检查点计时**。作者代码的 `exec_task` 也将输入准备与结果解析计入执行阶段，但本复现仍有实现与计时边界差异，所以图 6 对照应标为近似复现。并行主实验的各 worker 秒数不能相加当作墙钟时间。求解器异常导致计时缺失时，`timing_complete` 为 `false`。

## 离线数据验收

每个服务器批次完成后，从项目根目录对**时间戳运行目录**执行只读检查。预测试的预期作业数需显式覆盖；正式 RQ1/RQ3 默认分别要求 20/4 个作业：

```bash
python3 reproduce/validate_outputs.py reproduce/output/preflight/rq1/<时间戳> --expected-jobs 4
python3 reproduce/validate_outputs.py reproduce/output/preflight/rq3/<时间戳> --expected-jobs 1
python3 reproduce/validate_outputs.py reproduce/output/server/<时间戳>
python3 reproduce/validate_outputs.py reproduce/output/rq3/<时间戳>
```

检查逐作业的计划、`manifest.json`、`summary.json`、`results.jsonl`、检查点和已选中保留的案例文件，核对唯一且连续的 case ID、互斥 finding 计数、独立 CF/OF 计数、初始化后任务和 RQ1 的真实父种子变异。RQ3 还从逐行计时重新计算四个互斥类别的秒数与占比，并要求无缺失计时。`solver` 与 `result_parse` 已包含在 `execution` 内，不会重复相加。缺失保留文件、统计不一致或作业未正常结束时返回退出码 1；仅执行异常在写文件前就失败的案例目录缺失作为警告。复制到本机的批次也可验收，工具按运行目录中的求解器/重复次数定位作业，不依赖服务器原绝对路径。探索性的初始化专用批次可加 `--allow-init-only`，但这不能作为正式放行依据。

## 当前实现

- **地图变异：**从上一任务的完整等待频率图计算 32×32 区域热度，选最多 3 个互不重叠的区域；按作者代码中的 2:2:1 权重生成均匀噪声、CA 或 DLA patch。作者选区代码漏了前缀和，本实现按你选定的论文语义使用真正的区域总等待量。CA 包含作者的 26 个预设，其中 8 个用 Perlin 初始化；随机噪声 patch 中作者不可达的 Perlin 分支仍不启用。DLA 目标障碍密度为 30%，边界从原地图继承并参与生长。每个区域最多尝试 50 次，失败尝试的地图状态延续至下一次尝试，并按作者代码权重做替换、并、交、差等组合。
- **智能体变异：**无引导模式按作者权重 10%/30%/20%/20%/20% 选择整组重建、部分起终点互换、重排目标、重排起点、随机移动端点；最后一项对每个智能体的起点和终点分别以 20% 概率尝试移动。引导模式从 critical score 的高等待/长路程智能体中加权抽取 10 次，按 2:3:3 权重尝试局部移动、shadow、counter-flow；再从高使用/高等待格抽取 5 次尝试 congestion。每个局部端点偏移取作者代码的 [-5, 4]，每次操作最多尝试 100 次。插入后随机移除同样数量的原智能体，保持 agent 总数；热点列表为空时用轨迹热度列表补位。`mutation` 字段记录四类成功尝试次数。作者代码另有 appointment/naughty 注入，按你的选择暂不纳入论文四类的复现。
- **变异概率：**智能体变异默认 0.5；其中引导模式默认 0.8，取自作者公开代码。它们可通过 `--agent-mutation-rate` 和 `--guidance-rate` 覆盖。
- **可行性过滤：**分别保证所有起点互不重复、所有终点互不重复，端点可通行，且每对起终点处于同一连通分量；不同智能体的起点与终点可以交叉重合，与作者任务检查一致。这个检查不是多智能体可解性的完整证明。
- **蜕变判据：**原任务分别进行输入顺序随机重排、水平镜像、垂直镜像、180° 旋转、全体起终点交换；原任务有解后，再根据轨迹热度在原路径外最多添加 30 个一步探测 agent 和 30 个障碍。交换起终点只参与可解性检查；其余六项参与 SOC 的 MD 指标。论文写 `MRrevOrd` 为随机排列，作者代码却固定反转，本实现按论文文字处理。
- **原任务未解时：**仍执行五项不依赖原解的 MR，以检查论文 C1 的双向可解性条件；作者代码会在原任务未解时提前返回。probe/obstacle 依赖原解轨迹，此时跳过。
- **probe 的 SOC 诊断：**probe 增加 agent 本身会增加总 SOC，因此 `results.jsonl` 另记 `probe_soc_above_original_plus_probe_steps`，表示扣除新增一步 agent 的基础成本后的差额；求解器找到更短路径时它也可能为负。该诊断不改变论文口径的 MD/OF。
- **结果验证：**检查求解器轨迹的起终点、移动、障碍、顶点冲突与边交换，按最终到达目标的时刻复算 SOC。默认 MD>25% 计为 OF；同组有解和未解并存计为 CF 候选。CF 和 OF 可以同时计数。

当前 `--mode mutate` 已加入作者代码的能量选种主干：从解轨迹提取 16 维地图/智能体/运行特征，计算四项 critical score；MR 违反次数超过软配额 5 后，权重每次乘 0.95；fitness 结合 critical score、相对 SOC 差、新颖度分档和任务长度因子。父种子按最高 fitness 选择，复用后 fitness 乘 0.9。`--novelty-search exact|annoy` 可选两种最近邻实现：本机精确搜索每次比较所有已有向量；服务器 Annoy 模式先用初始池建立 50 棵树，之后按作者 200/10%/1000 条新向量的阈值定期重建，查询只搜索已建树向量，等待重建的缓冲区不参与查询。Annoy 的 0.5/2.0 初始阈值在索引达到 20 条后按树内每个向量的第二近邻距离计算第 25/75 百分位；本机精确模式仍用查询历史的第二近邻估计阈值。两种模式共用标准化欧氏距离与能量公式。由于 ANN 是近似检索，选种轨迹可能与精确模式不同；初始池建立前的少量查询仍使用精确搜索。

Critical Score 的“尚未到达目标”判断按论文语义使用 OR（任一坐标仍不同即计入）；作者代码写成 AND，疑似会漏掉部分路径点。presentation 中应说明这里按语义修正了作者代码的条件。初始 OF 任务可入种子池与初始特征索引，但初始阶段不更新 MR 权重；初始 CF/异常任务不入池或初始索引。后续变异产生的 OF/CF 失败任务不重新入父种子池，但只要原任务轨迹可分析仍加入新颖度索引。16 维字段与顺序对齐作者实现，但部分动态特征统计口径按论文语义处理：实际移动距离包括最后一步进入目标，而作者统计核函数排除了该目标步。随机 fallback 按本课程实现视作新初始任务，这是作者初始池流程的简化。

地图 patch、CA/DLA 和 Annoy 阈值已按作者实际运行路径补齐；热点区域求和、Critical Score 的 OR 与随机重排按你选定的论文语义处理。论文之外的作者额外智能体操作、RQ2/RQ4 与 RQ1 补充实验仍不在本轮范围。正式结果应标明课程复现范围及 RQ1 的 6 worker 与论文 32 worker 的差异，不能声称同算力重做了论文 Table 1。

## 输出

- `manifest.json`：运行参数、地图、随机种子与 MR 列表。
- `results.jsonl`：每组任务的父任务编号、变异操作、最多八次执行结果、CF/OF 标记、MD、critical score、MR 权重、多样性权重与 fitness。
- `summary.json`：互斥分类计数，另含独立的 `cf_count`、`of_count`。
- `case_XXXXX/{original,swap_start_goal,order,left_right,up_down,rotate_180,add_probe,add_obstacle}/`：各任务的地图、输入与求解器结果文件。原任务未解时，依赖原解轨迹的后两项不会生成。

PIBT 的输入文件引用 `input.map`；单独重放时需要将存档地图临时放到 PIBT2 的 `map/` 目录并保持文件名。LaCAM3 可直接引用存档的地图与场景。运行前先按项目根目录 [README](../README.md) 编译求解器。
