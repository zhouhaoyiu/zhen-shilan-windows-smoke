# 震·时澜 Windows 云端烟雾测试

该工作流在 GitHub Actions 的 `windows-latest` x64 runner 上验证精简部署流程，不上传业务事件、原始强震记录或完整离线运行时。

## 验证范围

- 使用 Python 3.12 x64 和带 SHA-256 的 Windows 依赖锁安装环境；
- 运行部署脚本单元测试和 Python 语法检查；
- 组装约 26 MB 的精简包，包含网站、全国三级行政区划和一个 K-NET 演示事件；
- 通过 `start_windows.bat` 启动服务；
- 验证首页、`/api/health`、文件夹监控状态、事件目录、演示事件和边界数据；
- 始终上传服务日志，并保留精简包作为短期 CI 产物。

## 触发方式

在 GitHub 仓库的 **Actions → Windows deployment smoke test → Run workflow** 手动运行。相关部署文件变更或拉取请求也会自动触发。

这个测试验证 Windows 环境中的安装、启动和网站服务接口。1.86 GB 完整离线包仍需在真实 Windows 机器上完成最终验收，因为精简测试不复制74个业务事件结果、24个完整 K-NET 目录和预装 Python 运行时。
