# 课程复现：任务变异与蜕变测试

这份独立实现使用 Python 标准库和仓库中已编译的 PIBT2 / LaCAM3 求解器。它能做随机基线实验，也能从求解轨迹出发生成变异任务。当前只实现论文 Approach 中的一部分代表性变异算子，便于先在服务器上跑通结果。

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

`--solver` 可选 `pibt`、`hca`、`pibt_plus`、`lacam3`。`--size` 将**整张**地图按最近邻方式缩放成指定大小；不传则保留原图。初始任务从一个足够大的连通区域抽取互不重复的起终点。`--cases` 指基础任务组数，每组最多运行原任务及 5 个蜕变任务。上面的 10 组仅用于演示，数量不足以比较论文中的失败率。

小规模预检可用 `--size 32 --agents 20 --patch-size 8`。论文配置采用 128×128、100 个智能体；作者公开代码的地图 patch 默认 32×32，因此正式试验可保留 `--patch-size 32`。

## 当前实现

- **地图变异：**从上一任务的轨迹找高频位置，在附近最多选择 3 个互不重叠的区域；按作者代码中的 2:2:1 权重生成均匀噪声、CA 或 DLA patch。DLA 目标障碍密度为 30%，边界从原地图继承并参与生长。每个区域最多尝试 50 次，并按作者代码权重做替换、并、交、差等组合。
- **智能体变异：**引导模式优先选择等待较多的智能体，在 2 格内移动一个端点；无引导模式包括随机交换部分起终点、重排目标、移动单个端点、重新生成任务。
- **变异概率：**智能体变异默认 0.5；其中引导模式默认 0.8，取自作者公开代码。它们可通过 `--agent-mutation-rate` 和 `--guidance-rate` 覆盖。
- **可行性过滤：**保证端点不同、端点可通行、每对起终点处于同一连通分量。这个检查不是多智能体可解性的完整证明。
- **蜕变判据：**原任务分别进行输入顺序重排、水平镜像、垂直镜像、180° 旋转、全体起终点交换。最后一种只参与可解性检查，不纳入 SOC 的 MD 指标。
- **结果验证：**检查求解器轨迹的起终点、移动、障碍、顶点冲突与边交换，按最终到达目标的时刻复算 SOC。默认 MD>25% 计为 OF；同组有解和未解并存计为 CF 候选。CF 和 OF 可以同时计数。

当前 `--mode mutate` 已加入作者代码的能量选种主干：从解轨迹提取 16 维地图/智能体/运行特征，计算四项 critical score；MR 违反次数超过软配额 5 后，权重每次乘 0.95；fitness 结合 critical score、相对 SOC 差、新颖度分档和任务长度因子。父种子按最高 fitness 选择，复用后 fitness 乘 0.9。新颖度用 StandardScaler 风格的标准化与精确暴力最近邻代替 ANN；索引不足 20 个时使用作者代码固定阈值 0.5/2.0，之后用历史第二近邻距离估计四分位阈值。该精确搜索用于课程规模，数据池很大时运行开销会高于 ANN。

Critical Score 的“尚未到达目标”判断按论文语义使用 OR（任一坐标仍不同即计入）；作者代码写成 AND，疑似会漏掉部分路径点。presentation 中应说明这里按语义修正了作者代码的条件。初始 OF 任务可入种子池与初始特征索引，但初始阶段不更新 MR 权重；初始 CF/异常任务不入池或初始索引。后续变异产生的 OF/CF 失败任务不重新入父种子池，但只要原任务轨迹可分析仍加入新颖度索引。16 维字段与顺序对齐作者实现，但部分动态特征统计口径按论文语义处理：实际移动距离包括最后一步进入目标，而作者统计核函数排除了该目标步。随机 fallback 按本课程实现视作新初始任务，这是作者初始池流程的简化。

地图 patch 包含均匀噪声、CA 和 DLA；按你的选择省略 Perlin，CA 规则集也采用代表性子集。其他引导式智能体操作仍待逐步加入。能量与反馈逻辑属于课程复现实现，ANN 索引和完整作者变异算子尚未补齐。结果应标为“课程简化复现”，不能声称已完整重做论文的 Table 1。

## 输出

- `manifest.json`：运行参数、地图、随机种子与 MR 列表。
- `results.jsonl`：每组任务的父任务编号、变异操作、六次执行结果、CF/OF 标记、MD、critical score、MR 权重、多样性权重与 fitness。
- `summary.json`：互斥分类计数，另含独立的 `cf_count`、`of_count`。
- `case_XXXXX/{original,order,left_right,up_down,rotate_180,swap_start_goal}/`：各任务的地图、输入与求解器结果文件。

PIBT 的输入文件引用 `input.map`；单独重放时需要将存档地图临时放到 PIBT2 的 `map/` 目录并保持文件名。LaCAM3 可直接引用存档的地图与场景。运行前先按项目根目录 [README](../README.md) 编译求解器。
