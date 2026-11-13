# 游戏更新后怎么办

游戏是自动更新的（自带更新器 + blake3 校验），所以**改 exe 的补丁方案会被覆盖**——本项目只做内存读写，不受影响，
但**偏移可能会变**。`src/offsets.py` 里的偏移总是对应当前最新的游戏，更新后核对一遍，变了就直接改成新的。

## 三步流程

### 1. 先自检

进一局，运行：

```bash
uv run --no-project --with numpy python src/verify_layout.py
```

它会读出当前这局的面板值，逐个偏移核对，然后告诉你：

```
the stat block matches offsets.STAT
VERDICT: offsets still valid
...
VERDICT: player offsets match the HUD
```

两段结论分别对应两层：

- 属性块：出现 `offsets still valid` → 不用动；出现 `no struct matched offsets.STAT` 或 `N structural field(s) moved` → 进第 2 步。
- 玩家对象（等级/金币/击杀/重抽/血量，与 HUD 对照）：出现 `player field(s) moved` → 按 [OFFSETS.md](OFFSETS.md) 第五节的差分法重新定位对应字段。
  放逐次数没有 HUD 可对照，要实际放逐一次、差分找出来。

### 2. 重新扫偏移

游戏 exe 里，属性是一个函数每帧写回结构体的。这个函数很好认：

```bash
uv run --no-project python src/scan_stores.py    # 扫 exe 里所有 movss [rsi+0x100..0x10000], xmm 写回点
uv run --no-project python src/group_stores.py   # 按 .pdata 函数边界分组，输出“包含最多属性写回”的函数
```

exe 路径作为第一个参数传入；不传时用环境变量 `AZS_EXE`，再没有就用在图形界面存档栏里选过的那个。仓库里不写默认安装路径。

输出形如：

```
func RVA 5ED1410..5ED27FB  stores=21  distinct=20
   disps: +0x230 +0x544 +0x568 ... +0x5F0 +0x5F4 ...
```

其中**连续 11 个 4 字节步进的偏移**就是属性块。

只看位移量和空位是否一致：属性块在 exe 里整段平移过好几次，只要空位（`+0x600`、`+0x610` 对应处）不变，块内布局就没变。
工具是以属性块本身为锚点定位的（玩家对象基址 = 属性块 − `0x5F4`），所以这种整体平移不用改任何偏移；
离属性块远的字段（击杀、放逐、局内计时）中间可能插了新字段，仍要进游戏核对。

经验值倍率是用 xmm13 写回的（`F3 44 0F 11 AE 9C 06 00 00`，多了一个 REX 前缀），`scan_stores.py` 认这种写法；
不认的扫描器会把这一处漏掉，看起来像少了一个字段，其实布局没变。

### 3. 对齐字段

新的偏移拿到后，用面板值逐个对照（`crit` 对应 0.xx、`cm` 对应 2.xx 这种），
或者用 `diffscan.py` 做差分确认。

只改 `src/offsets.py` 一个文件：属性块改 `STAT`，玩家对象改 `PLAYER`。
`trainer.py`、`player.py`、`verify_layout.py` 都从这里读偏移。改完跑一遍测试再进游戏验证：

```bash
uv run --no-project --with numpy --with lupa python -m unittest discover -s tests
```

还没核对过的玩家对象字段，先放进 `offsets.PLAYER_UNVERIFIED`：界面显示“?”，工具不会写它。

## 极端情况

如果 `scan_stores.py` 扫不到连续的 11 个属性写回，说明游戏把属性写回改成了别的指令形态
（比如 SSE `movss` 改成 AVX `vmovss`），此时要改 `scan_stores.py` 里的特征：

```
F3 [40/42/44/46] 0F 11 <modrm 0x86/8E/96/9E/A6/AE/B6/BE> <disp32>
```

- `F3 0F 11` = `movss m32, xmm`
- 中间可选的 REX 前缀只允许 R/X 位（R 表示 xmm8~15）；带 B 位会把 rsi 换成 r14，带 W 位不是普通的 movss，都不算
- modrm 的 `mod=10, rm=110` 表示 `[rsi + disp32]`
- `disp32` 落在 `0x100 ~ 0x10000` 且 4 字节对齐

## 极速模式

极速模式用玩家对象里的 `offsets.RUN_TIME`（`+0xFC4`）。游戏更新后那里不再是局内计时时，
[`src/turbo.py`](../src/turbo.py) 会自己发现（计时停住超过 60 秒、和 HUD 对不上）并改为按 HUD 时间在内存里找，所以照样能用，只是第一次打开要等约 1 分钟。
要重新定这个偏移，进一局正常战斗，跑：

```bash
uv run --no-project --with numpy python src/find_runtimer.py --test
```

它会列出候选，逐个 +10 秒再减回去，最后报出“计时变量”的地址；
再用 `uv run --no-project --with numpy python src/player.py --show` 看玩家对象的地址（`player object @ 0x...`），两者相减就是新的 `RUN_TIME`。找不到时看：
- 计时的类型变了：`turbo.KINDS` 目前试 f32、f64、Duration 三种；
- 拷贝变少了：`probe()` 提前停下的条件是“带着至少三分之一的候选一起跳”。

**注意**：`WOW_FIXED_DT` 环境变量看起来像速度开关，但实测只有 1.00x——exe 自己的日志写着它钉的是 **animation clock**（动画时钟），不是游戏时钟。
改 Bevy 的 `Time<Virtual>.relative_speed` 能让整个游戏变速，但局内计时不跟着变，没采用（见 [OFFSETS.md](OFFSETS.md) 第三节）。
