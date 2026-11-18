# MAPF-Fuzz 课程复现

这是课程 presentation 使用的精简复现仓库。自己的实验代码在 `reproduce/`，论文与作者案例仅作为参考。当前已能生成随机任务或从已有轨迹变异任务，运行四个 MAPF 求解器、验证轨迹并计算基础蜕变测试的 SOC 差异。

## 目录

| 路径 | 内容 |
|---|---|
| `reproduce/` | 自己的独立 Python 复现代码与操作说明 |
| `solvers/pibt2/` | PIBT、HCA、PIBT+ 求解器 Git 子模块 |
| `solvers/lacam3/` | LaCAM* 求解器 Git 子模块 |
| `data/maps/` | 作者公开的 30 张初始地图 |
| `reference/defects/` | 作者公开的 10 组案例，可用于对照与 presentation |
| `paper/` | 论文与补充材料 |
| `environment.yml` | 可选 Conda 环境，含 Python 与编译工具 |
| `solver-lock.json` | 本仓库采用的求解器版本记录 |

当前完成度、论文配置差异及小规模种子影响见 [复现进度](reproduce/PROGRESS.md)。

## 安装与编译

本机精确最近邻模式只用 Python 标准库；服务器 ANN 模式另需 Annoy。服务器还需要 Python 3.11、CMake、C++ 编译器和 Git；如需 Conda 环境：

```bash
conda env create --prefix .conda/envs/mapf-fuzz --file environment.yml
conda activate "$PWD/.conda/envs/mapf-fuzz"
python3 -m pip install -r reproduce/requirements-server.txt
```

初始化并编译求解器：

```bash
git submodule update --init solvers/pibt2 solvers/lacam3
git -C solvers/pibt2 submodule update --init third_party/googletest third_party/grid-pathfinding
git -C solvers/lacam3 submodule update --init --recursive
cmake -S solvers/pibt2 -B solvers/pibt2/build -DCMAKE_CXX_FLAGS="-include cstring"
cmake --build solvers/pibt2/build --parallel
cmake -S solvers/lacam3 -B solvers/lacam3/build
cmake --build solvers/lacam3/build --parallel
```

上面的 `-include cstring` 用于当前固定版本在 GCC 15 下的编译兼容。编译产物为 `solvers/pibt2/build/mapf` 和 `solvers/lacam3/build/main`。

## 本机预检入口

```bash
python3 reproduce/run_local.py --solver all --cases 3 --seed 1
```

此入口固定为 128×128、50 个智能体、单进程，用于本机预检；`--solver` 可选 `all`、`pibt`、`hca`、`pibt_plus` 或 `lacam3`。每次运行会创建独立的时间戳目录 `reproduce/output/local/`，不会覆盖之前的本机结果。

`run_pairs.py` 是本机与服务器共用的实验核心；本机入口固定单进程、精确最近邻并保留全部案例文件。服务器入口 `reproduce/run_server.py` 默认先打印论文主实验计划，只有添加 `--execute` 才开始运行，结果写入独立的 `reproduce/output/server/`。服务器使用 Annoy 索引，按作者代码每张地图最多生成 3 个初始任务，保留全部 JSONL 统计及失败案例文件，并每 500 组保留一组正常案例。断点续跑、批量统计和服务器规模性能验证尚未完成，因此正式长跑前还需完成这些步骤。更多参数和当前实现范围见 [reproduce/README.md](reproduce/README.md)。
