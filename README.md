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

代码只用 Python 标准库。服务器有 Python 3.11、CMake、C++ 编译器和 Git 即可运行；如需 Conda 环境：

```bash
conda env create --prefix .conda/envs/mapf-fuzz --file environment.yml
conda activate "$PWD/.conda/envs/mapf-fuzz"
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

## 先运行小实验

```bash
python3 reproduce/run_pairs.py --solver pibt \
  --map solvers/pibt2/map/random-64-64-10.map \
  --size 32 --agents 20 --cases 10 --seed 1 \
  --output reproduce/output/pibt-demo
```

加入 `--mode mutate --patch-size 8` 即可在这个 32×32 小样例上运行局部地图和智能体变异。换 `--solver hca`、`--solver pibt_plus` 或 `--solver lacam3` 可测试其他求解器。逐任务记录保存在 `reproduce/output/`，该目录不会提交到 Git。更多参数和当前实现范围见 [reproduce/README.md](reproduce/README.md)。

后续按课程展示需要，加入能量引导的种子选择、更多变异算子、批量运行和结果图表。
