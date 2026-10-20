# 碧蓝航线立绘导出器（三合一）

把原本需要 **AssetStudioMod → AzurLanePaintingExtract → AzurLanePaintingAnalysis-Kt** 三个软件、五六步手工操作的流程，合并成一个可以直接运行的 Windows 程序。

程序直接读取游戏的 `AssetBundles`，自动完成：

1. 解析 AssetBundle（相当于 AssetStudioMod 里筛选 Mesh / Texture2D 并导出）；
2. 用网格把贴图还原成图片（相当于 AzurLanePaintingExtract）；
3. 按游戏内的 UI 布局把多个部件合成完整立绘，并把 `paintingface` 里的差分表情合成到脸上（相当于 ALPA）。

最终每个立绘输出一张完整 PNG；差分表情可以像 ALPA 一样先看列表、再勾选要导出的那几个。

![界面](docs/screenshot.png)

## 功能特性

- **一步到位**：选好 `AssetBundles` 和输出目录，勾选立绘即可批量导出，不需要任何中间文件夹。
- **完整立绘**：自动合并背景、人物、舰装等多层 MeshImage，按游戏内布局定位。
- **差分表情自选**：`0` = 无表情底图，`1/2/3…` = 对应表情，可多选、可预览、可一键应用到全部立绘。
- **表情偏移微调**：横向/纵向偏移，支持输入与上下箭头，方向与 ALPA 一致（右/上为正）。
- **预览可缩放**：滚轮缩放、拖动平移、`1:1` / `适应窗口`。
- **断点续跑**：按“本次应产出的文件”逐个判断，缺哪个补哪个，已存在的文件不覆盖。
- **绿色免安装**：打包版自带 Python / Qt / VC 运行库，解压双击即用。

## 快速开始（使用打包版）

1. 解压整个文件夹到任意位置。
2. 双击 `AzurLanePaintingExport.exe`。
3. **素材根目录** 选择游戏的 `AssetBundles` 文件夹（例如 `D:\AzurLane\AssetBundles`），该目录下应能看到 `painting`、`paintingface`、`dependencies`。
4. **导出目录** 选择保存位置。
5. 在左侧列表勾选立绘，点 **开始导出**。

> 第一次打开时两个路径都是空白的，需要自己选择；选好之后程序会记住，下次启动自动填入。

## 从源码运行

需要 64 位 Python 3.11+（Windows 10/11）。

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app\main.py
```

也可以双击 `run.bat`（会调用系统里的 `python`）。

## 使用说明

### 差分表情

![差分表情](docs/faces.png)

- 选中立绘后，右侧列出 `0、1、2、3…` 的缩略图，数字即 ALPA 里看到的表达式编号。
- **`0` 号 = 无表情底图**：点 0 预览原画，勾 0 就导出 `2b.png`。
- **默认只勾 0**：默认导出一张原画底图；想看/导出表情时再勾 `1、2、3…`。
- 勾选哪些就只导出哪些；一个都不勾时按“底图”处理。
- **应用到全部**：把当前勾选套用到所有立绘（例如只勾 3 号）。
- 没有对应表情的立绘会自动跳过表情部分，不会报错。

### 表情偏移

- 缩略图下方有 **横向偏移 / 纵向偏移**，可直接输入数字，也可点上下箭头逐格调整。
- 方向与 ALPA 一致：横向正数向右，纵向正数向上。

### 筛选与勾选

- 搜索框支持名称关键字，例如 `hailunna`、`_rw`、`2b`。
- 过滤器：`全部` / `仅含差分表情` / `仅 _rw` / `仅 _n` / `排除背景部件`。

### 输出结构

所有文件放在以立绘名命名的子文件夹中：

```
导出目录/
├─ 2b/
│  ├─ 2b.png                 无表情底图（勾选 0 号时导出）
│  ├─ 2b_1.png               带 1 号差分表情的完整立绘
│  ├─ 2b_2.png
│  ├─ …
│  └─ parts/                 （勾选“同时导出各部件”时才有）
│     ├─ 2B.png
│     ├─ 2B_rw.png
│     └─ …
└─ hailunna/
   ├─ hailunna_1.png
   └─ …
```

### 导出选项

- **分辨率**：
  - `原始纹理（推荐）`：按素材本身分辨率输出，和 ALPE 导出的尺寸一致（如 2048×1800）。
  - `游戏布局（更大）`：按 prefab 的 UI 坐标 1:1 输出，少数 prefab 会更大（如 4299×3779）。
- **PNG 压缩**：0–9，默认 **0（不压缩，保存最快）**；需要更小体积可调到 6–9。
- **并行**：同时解析的进程数，默认一半 CPU 核心。
- **同时导出各部件**：额外导出每个图层。
- **裁剪透明边**：去掉四周透明区域。
- **跳过已存在的文件**：断点续跑，见下节。

### 断点续跑（跳过逻辑）

对每个立绘先算出“本次应该产出的文件”（`0` → `2b.png`，`1` → `2b_1.png`…），然后：

1. 全部已存在 → 跳过，连 bundle 都不打开；
2. 缺哪个补哪个 → 只打开一次，只写缺失的文件，已有文件不覆盖；
3. 该立绘没有的编号（根据缓存的表情名清单判断）→ 不触发解析，状态显示“无匹配表情”；
4. 勾选“同时导出各部件”时，还要求 `parts` 子目录里有 PNG 才算完成。

判断本身只是文件存在性检查，全量约 0.2 秒，主要耗时仍在真正需要补的立绘上。

## 命令行批处理（可选）

```bat
AzurLanePaintingExport.exe --cli ^
  --root "D:\AzurLane\AssetBundles" ^
  --out  "D:\导出\立绘" ^
  --workers 8
```

| 参数 | 说明 |
| --- | --- |
| `--name 2b` | 只导出指定立绘，可重复 |
| `--limit 50` | 最多导出多少个 |
| `--no-faces` | 不导出差分表情 |
| `--face-names 0,3` | 指定导出底图（0）和 3 号表情 |
| `--base` | 在全部表情之外额外导出底图 |
| `--parts` | 额外导出各部件 |
| `--crop` | 裁剪透明边 |
| `--ui-scale` | 按游戏布局尺寸输出 |
| `--level 6` | PNG 压缩等级（默认 0） |

> 命令行不加 `--face-names` 时会导出该立绘的所有表情。

## 工作原理

| 原工具 | 本程序的实现 |
| --- | --- |
| [AssetStudioMod](https://github.com/aelurum/AssetStudioMod) | `app/core.py` 的 `AssetSource` / `analyze()`：用 UnityPy 读取 SerializedFile，解析 GameObject / RectTransform / MonoBehaviour（`m_Sprite`、`mMesh`、`mRawSpriteSize`），取得顶点、UV、三角形与贴图 |
| [AzurLanePaintingExtract](https://github.com/azurlane-doujin/AzurLanePaintingExtract-v1.0) | `rebuild_part()`：按三角形 UV 包围盒裁剪贴图，再按网格坐标贴回画布（y 轴翻转与取整规则一致） |
| [AzurLanePaintingAnalysis-Kt](https://github.com/Deficuet/AzurLanePaintingAnalysis-Kt) | `ExtTransform`（移植其 RectTransform 坐标算法）、`layout()`、`compose_base()`、`load_faces()`、`compose_face()` |

差分文件匹配规则与 ALPA 一致：立绘名按 `_` 切分最多 3 段，取前两段并去掉 `_n`，作为 `paintingface/<名字>`。例如 `aogusite_4_n → paintingface/aogusite_4`。

## 项目结构

```
├─ app/
│  ├─ main.py          GUI、预览、导出调度
│  ├─ core.py          AssetBundle 解析 / 网格还原 / 布局合成
│  ├─ worker.py        多进程任务入口
│  ├─ build.ps1        PyInstaller 打包脚本
│  ├─ make_icon.py     生成程序图标
│  ├─ assets/          图标
│  └─ 使用说明.txt
├─ docs/               README 截图
├─ requirements.txt
├─ run.bat
└─ LICENSE
```

## 构建打包版

```powershell
pip install -r requirements.txt
cd app
.\build.ps1
```

生成结果在 `app\dist\AzurLanePaintingExport\`。需要 Windows 64 位与 Python 3.11。

## 常见问题

**1. 某些立绘导出失败 / 提示“该文件不是立绘 prefab”**
`painting` 目录里混有贴图 bundle（例如 `2b_rw`），它们没有 GameObject，无法单独成图，跳过即可。

**2. 导出图只有背景 / 只有舰装**
部分 prefab 的图层是给不同界面复用的，程序按 prefab 实际布局叠合；可用“同时导出各部件”自行取舍。

**3. 差分表情位置有偏移**
极少数老立绘依赖运行时脚本。程序按 RectTransform 计算，和 ALPA 一致；仍有个别偏差时可用偏移微调。

**4. 内存占用高 / 速度慢**
每个立绘要解码 1–4 张 2048 级贴图。批量导出时把“并行”调到 4–6，8 GB 内存建议不超过 6。

**5. 杀毒软件报毒**
PyInstaller 打包的未签名 exe 常见误报，源码完全公开，可自行构建。

## 开源协议与致谢

本程序以 MIT 协议开源，见 [LICENSE](LICENSE)。

算法与思路来自以下三个 MIT 项目，在此致谢：

- [aelurum/AssetStudioMod](https://github.com/aelurum/AssetStudioMod) — AssetBundle / Mesh / Texture2D 解析
- [azurlane-doujin/AzurLanePaintingExtract-v1.0](https://github.com/azurlane-doujin/AzurLanePaintingExtract-v1.0) — 网格还原算法
- [Deficuet/AzurLanePaintingAnalysis-Kt](https://github.com/Deficuet/AzurLanePaintingAnalysis-Kt) — 布局分析与差分表情合成

依赖：[UnityPy](https://github.com/K0lb3/UnityPy)（MIT）、[Pillow](https://python-pillow.org/)（HPND）、[PySide6](https://www.qt.io/)（LGPLv3）。

游戏《碧蓝航线》的角色立绘、差分表情等素材版权归上海蛮啾网络科技 / 勇仕网络及原画作者所有。本程序不附带任何游戏素材，仅供个人学习与素材整理，请勿用于商业用途。

## 更新记录

- **v1.10.0**：跳过逻辑改为“按需文件判断”，缺哪个补哪个；表情名字清单随数量一起缓存。
- **v1.8.0**：表情列表新增 0 号（无表情底图），默认勾 0；`--face-names 0` 导出底图。
- **v1.5.0**：PNG 压缩默认 0；新增表情横/纵偏移微调。
- **v1.1.0**：新增差分表情自选。
- **v1.0.0**：三工具合并，一步导出完整立绘与差分表情。
