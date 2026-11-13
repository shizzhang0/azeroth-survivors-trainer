<p align="center"><img src="docs/icon.png" width="128" alt="图标"></p>

<h1 align="center">Azeroth Survivors 修改器</h1>

<p align="center"><b>Azeroth Survivors</b> 的单机修改器：改属性、改状态、锁定数值，还有让首领提前到来的极速模式。</p>

纯内存读写，不改 exe、不注入代码，退出游戏就还原。需要 [uv](https://docs.astral.sh/uv/)，命令都在仓库根目录运行。

## 能改什么

| | 内容 |
|---|---|
| **属性** | 生命值、伤害、急速、暴击、护甲、范围、幸运……共 22 项，和游戏角色面板一样的写法，勾“锁定”持续生效 |
| **状态** | 金币、等级、经验、击杀、重抽、放逐、宝箱价格，写一次就生效；回满血、锁满血（血量一直等于生命值，想要更多血就锁生命值） |
| **极速模式** | 局内计时 2x / 4x / 8x：首领、波次提前到来，零散小怪也跟着加快，战斗速度不变；每局开始时自动关闭 |
| **存档** | 特权重置清零、各职业印记（只在游戏关闭时改，每次先备份） |

## 图形界面

双击 **`gui.bat`**，进入一局后点 **定位游戏**，等 30～90 秒。

按游戏的写法填数（伤害 `100` 就是 +100%），回车或勾“锁定”。角色在内存里搬家时会自动重新定位，锁定保留。

## 游戏内面板

在游戏目录（`AzerothSurvivors.exe` 旁边）放一个 `AZS-Launcher.bat`，把 `TRAINER=` 改成仓库路径，以后都用它启动游戏：

```bat
@echo off
set "TRAINER=C:\path\to\azeroth-survivors-trainer"
if not exist "%TRAINER%\remote.bat" (echo Trainer repo not found: %TRAINER% & pause & exit /b 1)
robocopy "%TRAINER%\addon\AZSRemote" "%~dp0benilla-config\AddOns\AZSRemote" /NJH /NJS /NFL /NDL >nul
if errorlevel 8 (echo Could not copy the addon into the game folder & pause)
start "AZS remote" /d "%TRAINER%" cmd /c remote.bat --preset "%~dp0AZS-preset.txt" --exit-with-game
start "" /d "%~dp0" "%~dp0AzerothSurvivors.exe"
```

开一局，点屏幕左侧的闪电图标（或 `/azs`）。图标右下角圆点：红 = 后台没开，黄 = 定位中，绿 = 可以用了。

**每局自动锁定**：编辑游戏目录里的 `AZS-preset.txt`，去掉行首 `#` 填数（如 `幸运 = 1000`），下一局生效。

## 命令行

```bash
uv run --no-project --with numpy python src/player.py --show          # 看状态
uv run --no-project --with numpy python src/player.py --gold 999999   # 改状态
uv run --no-project --with numpy python src/trainer.py --pin luck=5.0 # 锁属性（原始值：5.0 = +500%）
uv run --no-project --with numpy python src/find_runtimer.py --speed 4  # 极速模式
uv run --no-project --with numpy python src/save.py --resets 0        # 存档：特权重置清零（先退出游戏）
```

各脚本 `--help` 看全部参数。

## 开发

```bash
uv run --no-project --with numpy --with lupa python -m unittest discover -s tests   # 测试
uv run --no-project --with numpy python src/verify_layout.py                       # 游戏更新后自检偏移
```

偏移和原理见 [docs/OFFSETS.md](docs/OFFSETS.md)，游戏更新后怎么办见 [docs/UPDATING.md](docs/UPDATING.md)，存档格式见 [docs/SAVE.md](docs/SAVE.md)。

## 说明

仅供单机自用，请勿用于联机。仓库不含任何游戏资源，图标为原创。游戏更新后可能失效，风险自负。MIT 许可，见 [LICENSE](LICENSE)。
