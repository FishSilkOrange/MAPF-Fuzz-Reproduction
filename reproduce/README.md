# 课程复现：任务变异与蜕变测试

这份独立实现使用 Python 标准库和仓库中已编译的 PIBT2 / LaCAM3 求解器。它能做随机基线实验，也能从求解轨迹出发生成变异任务。当前已覆盖论文 Approach 中四类无引导与四类引导智能体变异；其他实验模块仍在逐步完成。

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

服务器小批量检查可用 `--solver pibt --repeats 1 --workers 2 --init-cases 2 --cases 4 --time-limit 60 --execute`。`--execute` 会实际运行；省略时只显示计划。当前没有断点续跑，Annoy 的服务器规模性能尚需实测，**正式长跑暂不可视为就绪**。

## 当前实现

- **地图变异：**从上一任务的轨迹找高频位置，在附近最多选择 3 个互不重叠的区域；按作者代码中的 2:2:1 权重生成均匀噪声、CA 或 DLA patch。DLA 目标障碍密度为 30%，边界从原地图继承并参与生长。每个区域最多尝试 50 次，并按作者代码权重做替换、并、交、差等组合。
- **智能体变异：**无引导模式按作者权重 10%/30%/20%/20%/20% 选择整组重建、部分起终点互换、重排目标、重排起点、随机移动端点；最后一项对每个智能体的起点和终点分别以 20% 概率尝试移动。引导模式从 critical score 的高等待/长路程智能体中加权抽取 10 次，按 2:3:3 权重尝试局部移动、shadow、counter-flow；再从高使用/高等待格抽取 5 次尝试 congestion。每个局部端点偏移取作者代码的 [-5, 4]，每次操作最多尝试 100 次。插入后随机移除同样数量的原智能体，保持 agent 总数；热点列表为空时用轨迹热度列表补位。`mutation` 字段记录四类成功尝试次数。作者代码另有 appointment/naughty 注入，按你的选择暂不纳入论文四类的复现。
- **变异概率：**智能体变异默认 0.5；其中引导模式默认 0.8，取自作者公开代码。它们可通过 `--agent-mutation-rate` 和 `--guidance-rate` 覆盖。
- **可行性过滤：**分别保证所有起点互不重复、所有终点互不重复，端点可通行，且每对起终点处于同一连通分量；不同智能体的起点与终点可以交叉重合，与作者任务检查一致。这个检查不是多智能体可解性的完整证明。
- **蜕变判据：**原任务分别进行输入顺序随机重排、水平镜像、垂直镜像、180° 旋转、全体起终点交换；原任务有解后，再根据轨迹热度在原路径外最多添加 30 个一步探测 agent 和 30 个障碍。交换起终点只参与可解性检查；其余六项参与 SOC 的 MD 指标。论文写 `MRrevOrd` 为随机排列，作者代码却固定反转，本实现按论文文字处理。
- **原任务未解时：**仍执行五项不依赖原解的 MR，以检查论文 C1 的双向可解性条件；作者代码会在原任务未解时提前返回。probe/obstacle 依赖原解轨迹，此时跳过。
- **probe 的 SOC 诊断：**probe 增加 agent 本身会增加总 SOC，因此 `results.jsonl` 另记 `probe_soc_above_original_plus_probe_steps`，表示扣除新增一步 agent 的基础成本后的差额；求解器找到更短路径时它也可能为负。该诊断不改变论文口径的 MD/OF。
- **结果验证：**检查求解器轨迹的起终点、移动、障碍、顶点冲突与边交换，按最终到达目标的时刻复算 SOC。默认 MD>25% 计为 OF；同组有解和未解并存计为 CF 候选。CF 和 OF 可以同时计数。

当前 `--mode mutate` 已加入作者代码的能量选种主干：从解轨迹提取 16 维地图/智能体/运行特征，计算四项 critical score；MR 违反次数超过软配额 5 后，权重每次乘 0.95；fitness 结合 critical score、相对 SOC 差、新颖度分档和任务长度因子。父种子按最高 fitness 选择，复用后 fitness 乘 0.9。`--novelty-search exact|annoy` 可选两种最近邻实现：本机精确搜索每次比较所有已有向量；服务器 Annoy 模式先用初始池建立 50 棵树，之后按作者 200/10%/1000 条新向量的阈值定期重建，未重建的新向量在小缓冲区精确搜索。两种模式共用标准化欧氏距离、新颖度分档和能量公式。索引不足 20 个时使用作者代码固定阈值 0.5/2.0，之后用历史第二近邻距离估计四分位阈值。由于 ANN 是近似检索，选种轨迹可能与精确模式不同；初始池建立前的少量查询仍使用精确搜索。

Critical Score 的“尚未到达目标”判断按论文语义使用 OR（任一坐标仍不同即计入）；作者代码写成 AND，疑似会漏掉部分路径点。presentation 中应说明这里按语义修正了作者代码的条件。初始 OF 任务可入种子池与初始特征索引，但初始阶段不更新 MR 权重；初始 CF/异常任务不入池或初始索引。后续变异产生的 OF/CF 失败任务不重新入父种子池，但只要原任务轨迹可分析仍加入新颖度索引。16 维字段与顺序对齐作者实现，但部分动态特征统计口径按论文语义处理：实际移动距离包括最后一步进入目标，而作者统计核函数排除了该目标步。随机 fallback 按本课程实现视作新初始任务，这是作者初始池流程的简化。

地图 patch 包含均匀噪声、CA 和 DLA；按你的选择省略 Perlin，CA 规则集也采用代表性子集。能量与反馈逻辑属于课程复现实现；ANN 索引已接入，但缓冲区处理与阈值统计仍是本课程实现，和作者代码不完全相同。结果应标为“课程简化复现”，不能声称已完整重做论文的 Table 1。

## 输出

- `manifest.json`：运行参数、地图、随机种子与 MR 列表。
- `results.jsonl`：每组任务的父任务编号、变异操作、最多八次执行结果、CF/OF 标记、MD、critical score、MR 权重、多样性权重与 fitness。
- `summary.json`：互斥分类计数，另含独立的 `cf_count`、`of_count`。
- `case_XXXXX/{original,swap_start_goal,order,left_right,up_down,rotate_180,add_probe,add_obstacle}/`：各任务的地图、输入与求解器结果文件。原任务未解时，依赖原解轨迹的后两项不会生成。

PIBT 的输入文件引用 `input.map`；单独重放时需要将存档地图临时放到 PIBT2 的 `map/` 目录并保持文件名。LaCAM3 可直接引用存档的地图与场景。运行前先按项目根目录 [README](../README.md) 编译求解器。
