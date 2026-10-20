# 第三方组件与协议说明

本程序（碧蓝航线立绘导出器）以 MIT 协议发布。其算法移植自以下三个 MIT 开源项目：

## 1. aelurum/AssetStudioMod
- 仓库: https://github.com/aelurum/AssetStudioMod
- 协议: MIT
- 用途: 提供 AssetBundle / SerializedFile / Mesh / Texture2D 的解析思路与字段含义。
  本程序用 UnityPy 完成等价解析，并在 `core.py` 中按其数据模型读取 `m_Sprite`、
  `mMesh`、`mRawSpriteSize`、RectTransform 层级等信息。

## 2. azurlane-doujin/AzurLanePaintingExtract-v1.0
- 仓库: https://github.com/azurlane-doujin/AzurLanePaintingExtract-v1.0
- 协议: MIT
- 用途: 提供“按三角形 UV 包围盒裁剪贴图并贴回画布”的网格还原算法。
  `core.py::rebuild_part()` 为该算法的等价移植（含 y 轴翻转与坐标取整规则）。

## 3. Deficuet/AzurLanePaintingAnalysis-Kt
- 仓库: https://github.com/Deficuet/AzurLanePaintingAnalysis-Kt
- 协议: MIT
- 用途: 提供 RectTransform 层级坐标计算（ExtendedTransform /
  ExtendedTransformChild）、图层合成顺序、差分表情定位与缩放规则。
  `core.py::ExtTransform`、`layout()`、`compose_base()`、`compose_face()` 为其等价移植。

## 运行时依赖

| 组件 | 协议 | 说明 |
| --- | --- | --- |
| UnityPy | MIT | Unity 资源解析与贴图解码 |
| Pillow | HPND | 图像处理 |
| PySide6 / Qt 6 | LGPLv3 | 图形界面 |
| texture2ddecoder / etcpak / astc-encoder | MIT / Apache-2.0 | 贴图解码后端 |

## 素材版权

游戏《碧蓝航线》（Azur Lane）的角色立绘、差分表情、背景等美术素材版权归
上海蛮啾网络科技、勇仕网络及原画作者所有。本程序不附带任何游戏素材，
仅提供解析用户本地游戏文件的工具，请勿将导出的素材用于商业用途。
