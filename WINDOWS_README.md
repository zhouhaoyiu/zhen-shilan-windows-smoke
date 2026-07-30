# 震·时澜：Windows 本机近实时地震波场推测系统

“震·时澜”是基于张世亮 ZSL 方法的近实时地震波场推测系统。网页入口处理单个新事件，批处理入口递归处理已有数据总目录；两种入口都读取 CSMNC 三分量记录、生成 ZSL 推测结果，并把已发布事件加入同一个网站目录。路径可以包含中文和空格。

## 1. 推荐：解压即用离线包

终端使用者只需要下载一次 `ZhenShiLan-Windows-x64-20260730-r4.zip`，完整解压到本机磁盘后双击：

```text
START_ZHEN_SHILAN.bat
```

不需要预装 Python、Conda、GMT 或 Node.js。离线包包含：

- Python 3.12.13 Windows x64 运行时；
- 固定版本科学计算依赖及本地 `wheelhouse`；
- 网站前端、K-NET 可视化演示和处理代码；
- 当前 2025/2026 年 74 个事件的网页可浏览成果；
- 省、市、县三级行政区 GeoJSON；
- 文件 SHA-256 清单和一键校验脚本。

首次启动会从包内 `wheelhouse` 安装固定版本依赖，不访问网络。完成标记保存在包内运行时目录，后续启动不会重复安装。需要先检查文件是否完整时，双击：

```text
deploy\windows\VERIFY_PACKAGE.bat
```

批量处理可以把数据总目录拖到 `PROCESS_DATA_FOLDER.bat`，也可以双击后粘贴目录路径。输出仍写入 `frontend\demo\batch`，原始数据目录不会被改写。

预置的 74 个事件保留网页展示所需 PNG、CSV、JSON、事件边界和 74 个可直接下载的完整结果 ZIP。事件目录中与结果 ZIP 重复的推测时程 DAT 不再另存一份，以避免同一数据重复占用约 2.42 GB。用户新导入或批量计算的事件仍按第 9 节规则在本机保存完整结果包。

### 复建发布包

正式包采用 Python 3.12.13 的 Windows x64 `python-build-standalone` 运行时、带 SHA-256 的固定版本 wheelhouse 和内置地图组装。维护者可在 macOS、Linux 或 Windows 执行：

```text
python deploy/windows/build_portable_windows.py --runtime-archive <python-build-standalone-windows-x64.tar.gz> --wheelhouse <windows-cp312-wheelhouse> --map-source deploy/windows/data/admin_geojson --output dist
```

组装脚本先核对运行时摘要，再生成逐文件 `MANIFEST.sha256`、最终 ZIP 和 ZIP 的 `.sha256`。目标用户不需要 Miniforge，也不依赖本机已有 Python。仓库保留的 `build_windows_offline.bat` 是必须在 Windows x64 上执行的 Conda 备用构建路径，不是正式包的目标机依赖。

原生 Windows 运行时必须在 Windows x64 构建。macOS 不能直接打包 Windows Conda 环境，但可以运行以下命令核验版本定义、入口文件和内置地图：

```text
python deploy/windows/verify_windows_runtime.py \
  --project-root . --map-root deploy/windows/data/admin_geojson --allow-host-python
```

## 2. 源码开发环境

安装 64 位 Miniforge。在 **Miniforge Prompt** 中进入项目目录并执行：

```bat
cd /d "D:\项目\zsl"
conda env create -f environment-windows.yml
```

环境文件不安装 GMT、PyGMT、GeoPandas 或 `watchdog`。文件夹监控使用 Python 标准库轮询，不需要额外监控依赖。已有同名环境时可执行：

```bat
conda env update -n zsl-windows -f environment-windows.yml --prune
```

## 3. 从网页导入单个事件

离线包直接双击 `START_ZHEN_SHILAN.bat`。源码开发环境也可以执行：

```bat
start_windows.bat
```

第 2 个参数可以指定端口，默认为 `8000`：

```bat
start_windows.bat "" 8080
```

启动后浏览器会自动打开。点击网页的“导入 EIdata/HNdata”，选择**单个事件的外层目录**；该目录内部至少包含 `EIdata`、`HNdata` 或 `ElData`，可以继续包含 `AccData`。不要选择同时装有多个事件的年份或月份目录。

选择事件后可以设置推测点：

- **规则网格**：分别设置南北向行数和东西向列数，每项 2–50，默认 10×10；
- **经纬度 CSV**：第一行为 `lon,lat` 或 `longitude,latitude`，此后每行一个点；
- **手动坐标**：每行输入一个 `lon,lat`。

单次最多 2500 个候选点。坐标按四位小数进入计算，重复点、非有限值和超出经纬度范围的点会在上传前提示。位于实测台站凸包外或不足 4 个自然邻站的点会写入目标点 QC 表并跳过，不会外推。不同推测点配置使用独立配置哈希和事件目录，不会覆盖已有结果。文件夹监控保持默认 10×10；需要自定义点时使用单事件网页导入。

浏览器只上传 `.dat` 文件。原始输入的保留副本和任务记录写入 `workspace\imports\<archive-id>\`，用户选择的源目录不会被改写。成功或失败记录都保留，便于追溯输入质量控制。

计算成果、事件目录、压缩包和归档索引统一写入 `frontend\demo\batch`；其中归档索引为 `archive.json`。网页导入、文件夹监控和批处理默认保存全部推测点三分量 DAT，并把它们放入事件 ZIP。命令窗口需要保持开启，按 `Ctrl+C` 停止网站。

浏览器打不开 `http://127.0.0.1:8000/` 时，先确认启动窗口仍在运行并出现“网站与归档服务已启动”。Windows 可执行 `netstat -ano | findstr :8000`；没有 `LISTENING` 结果表示服务未启动或已经停止。检查启动窗口中的 Python、天地图路径或端口错误，也可以把第 2 个参数改成 `8080` 后重试。

## 4. 文件夹监控

`start_windows.bat` 的可选第 3 个参数是监控根目录：

```bat
start_windows.bat "" 8000 "D:\应急处理"
```

服务递归识别 `EIdata`、`HNdata`、`ElData` 及其子目录。默认只处理监控启动后新增的事件，或启动后内容发生变化的事件；已有且未变化的事件不会自动重算。文件连续 15 秒无变化后才进入队列，避免读取仍在复制的记录。

网页的“文件夹监控”对话框点击“选择目录”会打开 Windows 系统文件夹选择器；选择后可以启动或停止监控，也可以决定是否处理启动时已经存在的事件。监控发现的原始数据先复制到 `workspace\imports`，再进入与网页手动导入相同的串行处理和归档队列。停止监控不会取消已经进入队列的任务。

轮询由本机服务的 Python 标准库实现，不安装或依赖 `watchdog`。

## 5. 行政区划地图资源

当前地图只绘制省、市、县行政区划，不绘制道路、铁路和水系。出图实际读取的是三个 EPSG:4490 GeoJSON，离线包已将其放在：

```text
data\
  admin_geojson\
    中国_省.geojson
    中国_市.geojson
    中国_县.geojson
```

服务按每个事件范围抽取三级边界，不会把全国 GeoJSON 整体送进浏览器。3.3 GB 原始图幅 ZIP 不参与当前行政区划渲染，因此没有重复放入离线包。`start_windows.bat` 仍兼容把外部图幅 ZIP 作为第 1 个参数传入，但正常使用不需要它。未找到内置行政区文件时才回退到全球陆地边界。

```bat
start_windows.bat "" 8000
```

## 6. 全量批处理

数据总目录可以同时包含 `2025应急处理`、`2026应急处理`、月份目录和事件目录。脚本会继续向下查找 `EIdata`、`HNdata` 及其 `AccData` 子目录。

只检查输入，不计算：

```bat
scan_windows.bat "D:\地震数据"
```

扫描、处理全部有效事件并启动网站：

```bat
run_windows.bat "D:\地震数据"
```

处理完成后浏览器打开 `http://127.0.0.1:8000/`。命令窗口需要保持开启；按 `Ctrl+C` 停止网站。重新运行时，输入快照和算法哈希都未变化的事件使用缓存。

`run_windows.bat` 保留为已有数据的全量批处理入口。第 3 个参数可传天地图 ZIP；没有元数据覆盖文件时，第 2 个参数保留为空字符串：

```bat
run_windows.bat "D:\地震数据" "" "E:\天地图\tianditu_250k_all_sheets_flat_20260712.zip"
```

同时使用元数据覆盖和天地图：

```bat
run_windows.bat "D:\地震数据" "D:\配置\event_overrides.json" "E:\天地图\tianditu_250k_all_sheets_flat_20260712.zip"
```

批处理完成后启动同一套本机网站，网页导入、归档和文件夹监控仍可继续使用。网站发布文件仍只写入 `frontend\demo\batch`。`scan_windows.bat` 只检查地震数据，不读取底图数据包。

## 7. 输入约束

每个 `.dat` 文件必须是 CSMNC ASCII 文本：前 16 行为固定头段，第 17 行起为加速度数组。头段至少包含发震时刻、震中、深度、震级、台站坐标、`EW/NS/UD` 分量、gal 等价单位、样点数和采样间隔。

同一台站只有在 `EW`、`NS`、`UD` 三个分量齐全，且台站坐标、样点数和采样间隔一致时才进入计算。一个事件至少需要 4 个坐标唯一且不共线的完整台站。空文件、重复分量、缺失分量和头段冲突会写入扫描报告，不会自动补值或按文件顺序错配。

## 8. 元数据冲突覆盖

先复制 `event_overrides.example.json` 为新的 JSON 文件。键必须是相对于数据总目录的事件路径，统一使用 `/`；只填写经过外部正式目录核验后确需覆盖的字段：

- `origin_time`：ISO 日期时间，例如 `2025-01-01 00:00:00`
- `latitude`、`longitude`、`depth`、`magnitude`
- `magnitude_type`
- `note`：必填，记录目录名称、版本或查询日期等可追溯依据

示例文件中的路径和值只是结构演示，不能直接用于计算。带覆盖文件运行：

```bat
scan_windows.bat "D:\地震数据" "D:\配置\event_overrides.json"
run_windows.bat  "D:\地震数据" "D:\配置\event_overrides.json"
```

原始波形不会被修改。覆盖值、依据说明、输入快照和算法哈希都会进入事件运行元数据。

## 9. 输出

`frontend\demo\batch` 中主要文件如下：

- `scan_report.csv`、`scan_report.json`：逐事件输入质量与排除原因
- `run_summary.json`、`catalog.json`：本次运行和网站事件目录
- `archive.json`：网页导入的成功与失败归档记录
- `events\<event-id>\`：`observed.csv`、`inferred.csv`、目标点 QC、8 张成果 PNG、完整 DAT、`event.json`、`run_metadata.json` 和 `pipeline_run.json`
- `packages\<event-id>.zip`：每个成功事件的完整结果包；打包时严格校验 DAT 文件名和数量

每个质控通过的推测点只在 `IF_folder` 保存一套完整三分量时程：

- `IF_folder\<经度4位>_<纬度4位>.<EW|NS|UD>.dat`：供后续 Step3/Step4 和按坐标读取。

因此每个推测点在事件目录和 ZIP 中共有 3 个 DAT 文件。四位小数坐标可避免自定义点在三位小数舍入后相互覆盖。只有明确需要节省磁盘时，才可直接运行 Python 并传 `--no-save-dat` 关闭完整时程归档。

网站展示实测/推测散点、空间渲染与等值线、PGA、PGV、PSA 0.3/1.0/3.0 s、仪器地震烈度和代表性三分量波形。成果页为六个指标分别生成一张双栏图：左栏是实测三角形与推测圆点，右栏是实测值锚定的观测—推测融合场；另有六指标距离图和候选点质控图，共 8 张 PNG。所有事件使用同一组固定强震分级阈值与颜色，不按单个事件的最小值和最大值重新拉伸色标。

## 科研边界

推测场是可复现的 ZSL 单次随机相位实现。基础种子由输入内容确定，每个分量和四位小数坐标再独立派生局部种子，因此改变网格密度或 CSV 行序不会改变重合坐标的随机相位。它属于事件处理产出，不等于独立精度验证。当前 K-NET 残差机器学习模型没有迁移到这些 CSMNC 事件；跨台网精度提升需要另做同口径、严格留出的验证。无效记录和无法解决的事件元数据冲突会被排除，不能为了增加事件数而人工补齐。
