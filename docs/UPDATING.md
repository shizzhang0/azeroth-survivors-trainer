# 游戏更新后怎么办

游戏是自动更新的（自带更新器 + blake3 校验），所以**改 exe 的补丁方案会被覆盖**——本项目只做内存读写，不受影响，
但**偏移会变**。

## 三步流程

### 1. 先自检

```bash
python src/verify_layout.py
```

它会读出当前这局的面板值，逐个偏移核对，然后告诉你：

```
layout that fits: v0.2.0-1118
VERDICT: offsets still valid
...
VERDICT: player offsets match the HUD
```

两段结论分别对应两层：

- 属性块：出现 `offsets valid` → 不用动；出现 `N structural field(s) moved` → 进第 2 步。
- 玩家对象（等级/金币/击杀/重抽/血量，与 HUD 对照）：出现 `player field(s) moved` → 按本文末尾的差分法重新定位对应字段。1039 的击杀数就是这样发现从 `+0x974` 移到 `+0xA3C` 的；放逐次数没有 HUD 可对照，1115 上是靠实际放逐一次、差分找到 `+0x1050` 的。

### 2. 重新扫偏移

游戏 exe 里，属性是一个函数每帧写回结构体的。这个函数很好认：

```bash
python src/scan_stores.py    # 扫 exe 里所有 movss [rsi+0x100..0x10000], xmm 写回点
python src/group_stores.py   # 按 .pdata 函数边界分组，输出“包含最多属性写回”的函数
```

exe 默认路径是 `C:\Program Files (x86)\AzerothSurvivors\AzerothSurvivors.exe`，装在别处时传参数或设环境变量 `AZS_EXE`。

输出形如：

```
func RVA 5ED1410..5ED27FB  stores=21  distinct=20
   disps: +0x230 +0x544 +0x568 ... +0x5F0 +0x5F4 ...
```

其中**连续 11 个 4 字节步进的偏移**就是属性块（1039 是 `+0x5F0 ~ +0x620`，1115 是 `+0x668 ~ +0x698`，1118 是 `+0x680 ~ +0x6B0`）。

只看位移量和空位是否一致：1115 整段平移了 +0x78、1118 又平移了 +0x18，空位（`+0x600`、`+0x610` 对应处）一模一样，说明块内布局没变。
工具是以属性块本身为锚点定位的（玩家对象基址 = 属性块 − `0x5F4`），所以这种整体平移不用改任何偏移；
离属性块远的字段（击杀、放逐）中间可能插了新字段，仍要进游戏核对。

### 3. 对齐字段

新的偏移拿到后，用面板值逐个对照（`crit` 对应 0.xx、`cm` 对应 2.xx 这种），
或者用 `diffscan.py` 做差分确认。

只改 `src/offsets.py` 一个文件：在 `STAT_LAYOUTS` **最前面**加上新版本的布局，再把 `GAME_VERSION` 指向它。
`trainer.py`、`player.py`、`verify_layout.py` 都从这里读偏移。改完跑一遍 `python -m unittest discover -s tests` 再进游戏验证。

> 对象级字段（金币/血/等级/重抽/放逐，`offsets.PLAYER`）若也位移了，用同样的差分法重新确认，
> 参考 `docs/OFFSETS.md` 第四节的思路：找“只减 1 的小整数”。

## 极端情况

如果 `scan_stores.py` 扫不到连续的 11 个属性写回，说明游戏把属性写回改成了别的指令形态
（比如 SSE `movss` 改成 AVX `vmovss`），此时要改 `scan_stores.py` 里的特征：

```
F3 0F 11 <modrm 0x86/8E/96/9E/A6/AE/B6/BE> <disp32>
```

- `F3 0F 11` = `movss m32, xmm`
- modrm 的 `mod=10, rm=110` 表示 `[rsi + disp32]`
- `disp32` 落在 `0x100 ~ 0x10000` 且 4 字节对齐

## 关于极速模式（未实现）

想真正改游戏速度，只能注入 DLL 去钩计时 API：

- `QueryPerformanceCounter`（Bevy 的时间来源）
- `GetTickCount` / `GetTickCount64` / `timeGetTime`
- `Sleep` / `SleepEx`（按倍率缩放）

参考实现：[OpenSpeedy](https://github.com/game1024/OpenSpeedy)（GPL-3.0）。

**注意**：`WOW_FIXED_DT` 环境变量看起来像速度开关，但实测只有 1.00x——
exe 自己的日志写着它钉的是 **animation clock**（动画时钟），不是游戏时钟。
