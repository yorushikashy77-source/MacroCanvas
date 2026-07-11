# MacroCanvas

MacroCanvas 是一个面向 Windows 的 PySide6 桌面宏映射工具。它可以把键盘、鼠标和窗口上下文组合成可复用的映射与宏动作，并通过 Kanata、winIOv2 或 Interception 处理输入输出。

## 主要能力

- 键盘与鼠标映射：按键、鼠标点击、滚轮、鼠标移动和等待动作。
- 宏动作编辑：支持动作树、循环动作、拖拽排序和动作级测试。
- 多种执行模式：执行一次、固定次数、按住循环、开关循环和无限循环。
- 录制与整理：录制键鼠输入，自动整理等待、滚轮和坐标动作。
- 配置档案：根据前台进程、窗口标题或匹配条件自动切换配置。
- 配置安全：原子保存、配置备份、损坏配置恢复和导入回滚。
- 输入后端：普通模式使用 Kanata/winIOv2，游戏模式可使用 Interception。
- 运行安全：单实例锁、前台窗口隔离、输入释放保护、诊断日志和协调式退出。

## 运行环境

- Windows；程序入口会主动拒绝在其他系统上运行。
- Python 3.12 或更高版本，建议使用 64 位 Python。
- PySide6 6.6 或更高版本。
- 如果使用游戏模式，需要另外安装 Interception 驱动。
- Kanata 和相关组件需要单独准备，仓库不包含第三方运行时二进制文件。

## 快速启动

在仓库根目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

如果 PowerShell 阻止虚拟环境脚本，可以直接使用：

```powershell
.\.venv\Scripts\python.exe main.py
```

## Kanata 与 Interception 组件

程序会按以下顺序寻找组件目录：

1. `MACROCANVAS_KANATA_DIR` 环境变量指定的目录；
2. 配置中保存的 Kanata 目录；
3. 可执行文件旁边的 `kanata` 目录；
4. `%LOCALAPPDATA%\MacroCanvas\kanata`；
5. 旧版兼容路径 `E:\kanata`。

普通模式至少需要对应的 Kanata 可执行文件和配置运行时。游戏模式还需要 `interception.dll` 以及已安装的 Interception 驱动。组件文件应从对应项目的官方发布渠道获取，不要把未经确认的 DLL 或驱动提交到仓库。

## 配置和数据位置

用户数据默认保存在：

```text
%LOCALAPPDATA%\MacroCanvas\
├─ config.json          主配置
├─ components.json      Kanata 组件目录设置
├─ config_backups\      配置快照
├─ diagnostic.log       诊断日志
└─ kanata*.log          输入后端日志
```

程序源代码目录不会保存用户配置。删除或移动源码不会自动删除上述用户数据。

## 开发与测试

安装开发依赖：

```powershell
python -m pip install -r requirements-dev.txt
```

运行全部测试：

```powershell
python -m pytest -q
```

只进行语法检查：

```powershell
python -m compileall -q main.py core config engine macro ui tests
```

测试配置已限制为 `tests/`，并排除 `旧版备份`、缓存、构建和发布目录，避免归档副本被重复收集。

## 打包

安装 PyInstaller 后执行：

```powershell
python -m pip install pyinstaller
pyinstaller MacroCanvas.spec
```

生成的程序位于 `dist\MacroCanvas.exe`。发布便携版时，请在可执行文件旁边准备 `kanata` 组件目录，并在目标 Windows 环境中验证输入后端、驱动和退出释放行为。

## 目录结构

```text
main.py                 程序入口和单实例控制
core/                   常量、路径和运行状态
config/                 配置模型、校验、保存和导入导出
engine/                 Windows 输入、Kanata 和 Interception 后端
macro/                  动作模型、录制整理和宏调度
ui/                     主窗口、编辑器、档案管理和运行生命周期
tests/                  回归测试、架构测试和安全测试
MacroCanvas.spec        PyInstaller 打包配置
pytest.ini              pytest 收集配置
```

## 使用注意

MacroCanvas 会监听并可能模拟键盘和鼠标输入。请只在自己拥有或明确获准控制的设备上使用，并在启用游戏模式、驱动或全局输入钩子前保存工作。修改映射后，建议先使用界面中的测试功能确认动作，再启动全局输入引擎。

## 当前状态

项目当前以 Windows 桌面使用和本地开发为主。仓库中的测试重点覆盖配置安全、录制整理、档案切换、输入后端生命周期、宏停止释放和界面交互回归。
