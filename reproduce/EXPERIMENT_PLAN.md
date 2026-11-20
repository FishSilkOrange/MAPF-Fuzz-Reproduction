# RQ1 / RQ3 服务器执行计划

本文件是课程 presentation 的执行清单。范围固定为 RQ1 主实验和 RQ3 效率实验；RQ2、RQ4、MET-MAPF 对照、迷宫补充、LaCAM* 30 秒复测和问卷暂不运行。正式数据一律使用 128×128 地图、100 agents、30 张输入地图和 Annoy；本机 50-agent 数据只用于已有的功能检查。

## 0. 正式长跑前冻结实现

1. 补齐作者代码中实际运行的 CA 预设（26 个，其中 8 个采用 Perlin 初始化）及 DLA 行为。随机 patch 的 Perlin 分支在作者代码中不可达，仍不启用。地图热点从完整等待频率图计算，按论文语义加前缀和，选择真正累计等待最多的区域；作者代码漏加前缀和。保持已确定的论文口径：MRrevOrd 随机排列、Critical Score 用 OR、probe/obstacle 纳入 MD；论文四类引导智能体变异保留，作者额外的 appointment/naughty 不纳入。
2. 核对 Annoy 新颖度分档及当前阈值实现和作者代码的差异；需要变更时在正式长跑前完成。若仍存在可影响 RQ1/RQ3 结果的差异，先向用户说明并确定口径。
3. 核心代码由主 agent 修改；每次代码修改由只读审查 agent 对照论文和项目运行检查，不通过则继续修复。CA 中的 Perlin 需要运行依赖；同步更新服务器依赖清单和隔离环境。可视化 agent 负责最小数据验证器及后续离线汇总/绘图。正式实验前用预测试输出验证统计与绘图读取链路；已确定的图表无需新增采集字段。
4. 主 agent 完成审查后的代码提交与推送；服务器更新到同一 Git 提交。记录提交号、地图 SHA-256、求解器二进制 SHA-256、Python 3.11/Annoy 版本。正式批次开始后不修改 `run_pairs.py`、`checkpoint.py`、地图或求解器二进制，以免检查点拒绝恢复。

## 1. 预测试

服务器已有 8 个 CPU 线程、30 GiB 内存；Python 3.11.18、Annoy 1.17.3 和两个求解器二进制已安装。全部命令从项目根目录运行，使用 `.venv/bin/python`。预测试输出放在 `reproduce/output/preflight/`，与正式 RQ1/RQ3 输出分开。

先在服务器 `.venv` 里直接运行一次真实 Perlin CA 和 DLA patch 烟雾检查。短跑只含少数地图变异，可能碰不到这两条分支；依赖导入成功也不足以证明它们能生成 patch。

```bash
.venv/bin/python - <<'PY'
import random
from reproduce.run_pairs import cellular_automata_patch, generate_map_patch
border = ("." * 32,) * 32
for name, patch in (
    ("ca_perlin", cellular_automata_patch(border, random.Random(1),
                                         (4, "Perlin", .3, "WalledCities"))),
    ("dla", generate_map_patch(border, "dla", random.Random(2))),
):
    assert len(patch) == 32 and all(len(row) == 32 for row in patch)
    assert all(set(row) <= {".", "@"} for row in patch)
    print(name, sum(row.count("@") for row in patch))
PY
```

### RQ1 全链路短跑

```bash
.venv/bin/python reproduce/run_server.py \
  --solver all --repeats 1 --workers 6 \
  --init-cases 6 --cases 12 --time-limit 120 --timeout 40 \
  --output-root reproduce/output/preflight/rq1 --execute
```

四个求解器都使用正式的地图大小、agent 数、Annoy、求解器超时及 MR；缩小的只有初始池、任务组上限、fuzzing 时限和重复次数。明确设置 `--init-cases 6`，保证 `--cases 12` 中有机会进入变异阶段。若六个初始任务都无可用种子，扩大预测试初始池后重试；这不改变正式规模。四求解器短跑完成后，另用 PIBT 100 组做了一次中断续跑演练：中断核心进程后，6 个孤儿 worker 未自行退出并继续占用运行锁；确认它们属于这一输出目录且父进程已消失后，向这 6 个 worker 发送 TERM，待它们退出，再对**同一个** `--resume-run` 目录续跑。最终 100 个 case ID 连续且无重复，验收器通过。两项短跑与续跑演练均在预算内完成。

### RQ3 计时短跑

```bash
.venv/bin/python reproduce/run_server.py \
  --profile rq3_efficiency --solver pibt --repeats 1 --workers 1 \
  --init-cases 2 --cases 6 --time-limit 120 --timeout 40 \
  --output-root reproduce/output/preflight/rq3 --execute
```

`run_rq3.py` 不开放 `--init-cases`，因此预测试用同一个核心服务器入口并显式指定 RQ3 profile。正式 RQ3 仍由独立的 `run_rq3.py` 启动。若只设 `--cases 4/10` 而保持默认初始池，任务可能全部属于初始化，无法验证 RQ3 的计时汇总。

### 放行条件

- 四个求解器均产出 `manifest.json`、`checkpoint.sqlite3`、`results.jsonl`、`summary.json`；二进制执行没有整体崩溃，`processing_error` 和 `dispatch_error` 均为空。
- 每个预测试作业的 JSONL 行数、唯一 case 数、各类 finding 之和与 `summary.completed_cases` 一致；有完成的初始化后任务，且 RQ1 至少出现一次真正选父种子的变异。原任务求解成功的组包含预期的七个 MR 结果。
- 已选中保留的正常样本或发现案例含地图、实例和求解器结果，可用于制作地图、轨迹及 SOC 对比图；若执行异常导致文件缺失则单独标记。
- RQ3 的 `rq3_timing.post_initialization_cases > 0`、`excluded_incomplete_cases = 0`；四个互斥 `category_percent` 合计约 100%。`solver` 和 `result_parse` 是 `execution` 的子项，不重复相加。
- 中断续跑后的 case ID 不重、不漏。记录实际每组耗时和初始化耗时；预测试未单独采集 CPU/内存峰值或精确磁盘增长，这两项由正式运行期间的系统监控补记。若预测试暴露实现问题，修复、审查并重做该短跑，**不启动正式长跑**。
- 真实 `.venv` 的 Perlin CA 和 DLA 烟雾检查均成功；主短跑是否随机命中这两条分支另作记录，不以命中作为预测试放行条件。
- 额外只读检查 30 张地图在 128×128/100 agents 下均能生成有效初始任务；实际生成 90 组，覆盖全部 30 张图。

### 本次预测试记录

- RQ1 四求解器短跑：`reproduce/output/preflight/rq1/20261120T034121.910307Z`，每求解器 12 组、其中 6 组初始化后任务；48 组均有原任务和七项 MR，验收器 0 错误、0 警告。四个求解器均实际进行了地图变异，未发现 CF/OF。这批仅覆盖排序最前的两张 Berlin 地图，故不用于推断正式发现率。
- RQ3 PIBT 计时短跑：`reproduce/output/preflight/rq3/20261120T034448.278529Z`，6 组中 4 组有完整的初始化后分阶段计时，验收器 0 错误、0 警告；因达到 case 上限，未跑满 120 秒，不能用于论文一小时效率对照。
- PIBT 断点续跑演练：`reproduce/output/preflight/resume/20261120T034620.469165Z`，在已有结果后中断主进程，清理同批次的孤儿 worker 后对原目录恢复；最终 100 组、case ID 0–99 连续，SQLite 中无待处理组，验收器 0 错误、0 警告。三批次的图表字段均经可视化 agent 独立核查，没有缺失。
- 服务器环境：Git `7428528`，Python 3.11.18，Annoy 1.17.3、noise 1.2.2、NumPy 2.2.6；30 GiB 内存、预测试前可用磁盘约 88 GiB。Perlin CA、DLA 和 Annoy 已在服务器 `.venv` 直接运行。正式启动前仍须核对账户余额与磁盘。

## 2. 正式运行

正式实验一次只运行一个入口，不同时运行 RQ1 和 RQ3，以免 8-CPU 机器上的资源争用影响 RQ3 单进程计时。使用 `nohup` 或持久终端启动，保存完整日志、进程号和运行根目录；SSH 断线不应终止作业。先执行 RQ3，确认成功后自动执行 RQ1：

```bash
set -e
.venv/bin/python reproduce/run_rq3.py --solver all --execute
.venv/bin/python reproduce/run_server.py --solver all --workers 6 --execute
```

- **RQ3：**四个求解器各 1 次，初始池后各计时 1 小时，单 worker，输出到 `reproduce/output/rq3/<时间戳>/`。
- **RQ1：**四个求解器各 5 次，PIBT/HCA/PIBT+ 每次最多 20,000 组或 1 小时，LaCAM* 每次最多 25,000 组或 6 小时，先到为止。初始池每图最多 3 组，30 张图实际最多约 90 组；主实验使用 6 worker（论文为 32），输出到 `reproduce/output/server/<时间戳>/`。不传 `--init-cases`、`--cases`、`--time-limit` 或 `--timeout` 覆盖正式默认值。
- 若进程意外终止，保留原目录。先用 `ps -eo pid,ppid,cmd` 核对旧 worker 的完整命令包含该批次的 `--output <原目录>`；如果主进程已消失、worker 仍持续占用运行锁，只向这些确认属于该批次的孤儿 worker 发送 `TERM`，并确认它们及关联的求解器子进程退出。随后使用对应入口的 `--resume-run <原时间戳目录> --execute` 继续；不创建新批次替代旧批次。若核心代码、地图或求解器必须改变，先停止并报告：这些变更会破坏同一批次的可比性和检查点校验。

按所有作业跑满时间上限估算，RQ1 为 45 小时，RQ3 为 4 小时，合计 49 小时、约 79.38 元（1.62 元/小时）；初始化、预测试及服务器空闲时间另计，任务组上限先到则运行更短。用户确认正式启动前账户余额约 **120 元**，并选择将启动门槛降为 **100 元**，保留 10 小时缓冲：49+10 小时约 95.58 元，按 120 元起算预计剩余约 24.42 元。正式启动前可用磁盘须至少 **50 GiB**。程序在每个作业结束后自动验收输出，验收失败即停止；首个作业前检查 50 GiB、其后每个作业前检查 20 GiB，低于阈值即停止。用户在长跑期间负责查看云平台**实际余额**、日志和磁盘，尤其在每个作业结束后；主 agent 不持续在线监控。若实际余额不足以覆盖未完成作业的时限加 10 小时缓冲，或运行中磁盘空闲低于 20 GiB，及时终止当前核心进程，按前述孤儿 worker 步骤清理并从原检查点恢复。保留失败案例和每 500 组一份正常样本的现有策略。

## 3. 数据验收、备份和演讲输出

1. 每个作业结束后核对 `summary.stop_reason` 为 `time_limit` 或 `case_limit`，`processing_error`、`dispatch_error` 为空；验证 JSONL 唯一 case ID、行数与统计一致。正式 RQ1 应有 20 个完整作业，正式 RQ3 应有 4 个。
2. RQ1 保存全部 `results.jsonl`、`summary.json`、`manifest.json`、`checkpoint.sqlite3` 和保留的 `case_XXXXX` 目录；RQ3 同样保存。每完成一个求解器，将该部分结果复制到本机或独立存储；不要只依赖服务器单盘。
3. 用 JSONL/summary 制作 RQ1 五次重复的 CF/OF 汇总、累计发现曲线、逐 MR 贡献、MD 阈值曲线、地图来源分布和新颖度/能量趋势。用保留的失败案例文件制作地图、路径及 SOC 对比；父子地图/起终点对照可借助 checkpoint 的种子任务。正常父任务若未保留文件，不制作其完整轨迹对照。若没有新发现，用现有作者案例作机制说明并明确标注其来源。
4. 用 `rq3_timing` 制作四个求解器的论文四阶段占比图、原始秒数与墙钟残余开销图。图 6 对照标注为近似：本复现结果解析还验证轨迹，且使用检查点。presentation 明确写出 RQ1 使用 6 worker（论文 32）、其余已决定的实现差异及实际每批次任务数；绝对失败数不作同算力的直接比较。

任何尚未批准的论文差异、预测试验收失败或输出字段缺口，都在正式长跑前解决。完成上述门槛后才冻结代码并启动正式作业。
