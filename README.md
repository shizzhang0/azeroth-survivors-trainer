# Azeroth Survivors Trainer

单机内存修改器，针对 **Azeroth Survivors**（Rust + Bevy 重写的 WoW 1.12 客户端）。

纯内存读写：**不改存档、不改 exe、不注入代码**。退出游戏即完全还原。

## 已验证版本

`v0.2.0-1118`（2026-11-04 实测：属性块全部字段、金币/血量/等级/经验/击杀/重抽/宝箱价格均与游戏 HUD 一致，放逐次数与实际放逐次数一致）。

游戏更新后偏移可能需要重新定位，见 [docs/UPDATING.md](docs/UPDATING.md)。

## 能做什么

| 层 | 脚本 | 改什么 | 会不会被覆盖 |
|---|---|---|---|
| **状态层** | `player.py` | 金币、当前血量、最大血量、等级、经验、击杀、重抽次数、放逐次数、宝箱价格 | 不会，写的是游戏真实状态 |
| **属性层** | `trainer.py` | 17 项运行时属性（伤害、暴击、暴伤、急速、移速、拾取、范围、经验倍率、护甲、回复、幸运、回响…） | 会，升级重算会覆盖，需要 `--pin` |

`--pin` 每次写入前都会重新校验目标：如果那块内存不再像属性块（这局结束了、内存被复用），就停止写入，不会往无关内存里乱写。

![demo](docs/demo.png)

## 快速开始

```bash
pip install numpy

# 看状态
python src/player.py --show

# 改钱 / 回血 / 无限重抽 / 宝箱价格
python src/player.py --gold 999999
python src/player.py --heal
python src/player.py --rerolls 99
python src/player.py --chest 1      # 下一个宝箱只要 1 金币（开完游戏会照常涨价，想再便宜就再改一次）

# 改属性（持续生效）
python src/trainer.py --pin dmg=4.0 --pin luck=5.0
```

游戏需要先启动并**进入一局**（不在菜单、人还活着）。脚本会自动定位进程和对象。

`--pin` 启动后要先扫两遍内存（30～90 秒，会打印进度），出现 `pinning every 0.02 s` 才开始写入，窗口关掉就停。
游戏每 1～3 秒会重算一次属性，pin 每 0.02 秒写回一次，面板偶尔闪回原值不超过约一帧（`--interval` 可调）。

## 原理

**两层结构**，这是本项目最核心的认知：

- 属性层改的是一块 `f32` 缓存（`hp/dmg/cd/crit/cm/spd/pick/xp/arm/?/reg/area/luck/gold` 共 20 个槽位），
  它会被游戏重算覆盖，所以必须高频重写（`--pin`）。
- 状态层改的是**玩家对象本体**（几 KB 的大结构），金币、血量、重抽、放逐、宝箱价格都在里面，写入即真实状态。

**定位不靠固定地址**，而是：

1. 用属性块的**结构指纹**（多个字段同时落在合理区间）找候选；
2. 用 HUD 里游戏自己打印的**击杀数 / 金币**交叉验证。

这样即使每次启动地址不同、甚至版本小更新，也能稳定定位。

偏移表见 [docs/OFFSETS.md](docs/OFFSETS.md)；代码里所有偏移只在 [src/offsets.py](src/offsets.py) 一处定义。

## 目录结构

| 文件 | 作用 |
|---|---|
| `src/offsets.py` | **所有偏移和合理区间**，游戏更新后只改这里 |
| `src/memscan.py` | 通用 x64 进程内存读写 + 分块扫描（也能单独当命令行用） |
| `src/game.py` | 找游戏进程、读取 HUD 数据 |
| `src/player.py` / `src/trainer.py` | 状态层 / 属性层工具 |
| `src/verify_layout.py` | 只读自检：偏移还对不对 |
| `src/scan_stores.py` / `src/group_stores.py` | 从 exe 里重新扫属性写回点（纯标准库） |
| `src/diffscan.py` | 差分扫描，找未知字段 |
| `tests/` | 离线测试，用假内存模拟进程，不需要开游戏 |

## 测试

```bash
python -m unittest discover -s tests -v
```

## 版本更新后

```bash
python src/verify_layout.py      # 先自检：偏移还对得上吗
```

对不上时，`scan_stores.py` + `group_stores.py` 会重新从 exe 里扫出属性写回点。
完整流程见 [docs/UPDATING.md](docs/UPDATING.md)。

## 已知限制

- **生命偷取 / 荆棘 / 生命数** 改不了：它们不存成数值，是每次从装备/天赋现算的（已验证：`0.006` 在整个进程里连精确的 float32 都不存在）。
- **极速模式**没做：需要注入 DLL 钩 `QueryPerformanceCounter`，风险高。环境变量 `WOW_FIXED_DT` 只影响动画时钟，不影响游戏速度（已实测 1.00x）。
- 只支持单人模式；联机不碰。

## 免责声明

- 仅供**单机自用**。请勿用于联机/合作模式，那会影响其他玩家。
- 仓库**不包含任何游戏资源**（MPQ、exe、美术资源）。
- 游戏更新后需自行重新定位偏移；不保证长期可用。
- 使用风险自负。

## 许可

MIT — 见 [LICENSE](LICENSE)。

## 参考

- 计时 API 缩放（未实现）的思路参考 [OpenSpeedy](https://github.com/game1024/OpenSpeedy)（GPL-3.0，本仓库未包含其代码）。
- 指令级结构定位的思路参考 iced-x86 反汇编器的用法（本仓库用机器码形态匹配代替，不依赖反汇编库）。
