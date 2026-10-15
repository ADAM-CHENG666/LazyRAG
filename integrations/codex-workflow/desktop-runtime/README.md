# Codex Desktop 共享后台运行组件

将已验证的实验适配器整理为独立运行组件，供后续 LazyMind 安装流程调用。当前支持 macOS、内置 CLI `0.162.0-alpha.17.2`。已接通 Workflow 原生 Continue/Stop 的入口与绑定能力；尚未接入设置页开关。2026-10-15 本机部署后，用户反馈 Continue/Stop 测试正常；不代表长期稳定性或跨版本兼容已验证。

```text
Desktop ──stdio── 适配器 ──WebSocket / Unix socket── 独立 App Server
                    │                                  │
                    └─ 原装 MCP 插件 ←─桥接 socket─ 工具代理
                             │                         ↑ stdio
                             └─原生 socket─→ Desktop   └─后台启动
```

原图标启动 Desktop 后，Desktop 通过 `CODEX_CLI_PATH` 启动适配器。适配器在内核锁下检查后台归属、缺席启动并等待就绪；已有后台直接复用。退出 Desktop 只清理本次适配器及插件，不停止共享后台。重开后工具代理连接新的桌面桥接，保留真实聊天元数据与原生审批。

## 构建与准备

开发机需要 pnpm；运行时使用 App 自带的 Node/CLI 和 Python 3（默认 `/usr/bin/python3`，仅使用标准库 `flock` 等能力）。准备过程会检查依赖，不自动安装任何软件。

```bash
pnpm --dir integrations/codex-workflow build:desktop
node integrations/codex-workflow/dist/desktop/prepare.mjs \
  --output "$HOME/Library/Application Support/LazyMind/codex-desktop"
```

`prepare` 复制构建产物，生成 `runtime.json`、`launch-adapter`。**只准备文件，不启用共享模式、不启动 Desktop 或后台。** 输出目录必须尚不存在；升级另选目录，并先停用旧安装，避免覆盖运行中版本。可传 `--app`、`--codex-home`、`--python`；缓存存在多个插件版本时，必须用 `--desktop-plugin`、`--browser-plugin` 指定当前已安装且路径已解析的 `.mcp.json`。

`runtime.json` 保存实际 App 路径、原插件配置、版本和运行目录；不保存 Desktop 动态校验身份。构建包不依赖实验目录或开发机 `node_modules`。安装目录应使用持久位置；下面的 `INSTALL` 仅是示例变量。

```bash
INSTALL="$HOME/Library/Application Support/LazyMind/codex-desktop"
node "$INSTALL/runtime/manage.mjs" plan --config "$INSTALL/runtime.json"
node "$INSTALL/runtime/manage.mjs" enable --config "$INSTALL/runtime.json"
```

`plan` 只展示变更。`enable` 保存原 GUI 环境，然后执行以下配置，不退出或打开 App：

| 修改项 | 作用 |
| --- | --- |
| `CODEX_CLI_PATH=<安装目录>/launch-adapter` | Desktop 启动外部适配器 |
| `CODEX_APP_SERVER_FORCE_CLI=1` | Desktop 使用 CLI stdio 通道 |
| 清除 `CODEX_APP_SERVER_WS_URL`、`CODEX_APP_SERVER_USE_LOCAL_DAEMON` | 避免跳过适配器直连后台 |
| `~/Library/LaunchAgents/com.lazymind.codex-desktop.plist` | 登录时设置上述环境，不预启动后台；`KeepAlive=false` |
| `<安装目录>/activation.json` | 保存原环境，供停用恢复 |
| `<Codex profile>/lazymind-shared-server.json` | 告知 Workflow dispatcher 和 MCP 绑定使用同一共享后台；停用时移除本安装登记 |

随后正常退出 Desktop，再点击原图标即可。无需替换图标、修改应用包、签名或安全校验。运行记录位于 `runtime.json` 的 `runtimeDir`，默认是当前用户专属的短 `/private/tmp/lm-codex-<uid>/<安装标识>` 路径，以满足 macOS socket 路径长度限制。

## 状态与恢复默认

```bash
node "$INSTALL/runtime/manage.mjs" status --config "$INSTALL/runtime.json"
# 先正常退出 Desktop，再执行：
node "$INSTALL/runtime/manage.mjs" disable --config "$INSTALL/runtime.json"
```

`status` 读取环境与后台身份记录，不会启动后台；记录存在不代表进程仍存活。`disable` 卸载本组件的登录服务、移除本组件的 Workflow 登记、还原原 GUI 环境，并将 Desktop 生成配置中属于本安装的 `CODEX_CLI_PATH` 恢复到原 CLI；其他编辑保留。配置被外部改动时明确报错，不覆盖未知配置。之后可从原图标打开默认模式。

停用不会终止后台任务，也不会删除聊天；后台可能仍存活，不应立即删除安装目录。正式卸载时的空闲确认和后台停止留待产品安装流程集成。此版本管理命令需串行调用。

## 实现边界

- 复用的是本安装记录并核验的后台，不自动接管其他 daemon。`backend.json` 中的 PID、启动时间、命令以及 socket 归属用于校验。
- 每个适配器独立登记、监听和清理桥接。同一 Desktop 上下文组可共享；多个不同 Desktop 上下文同时存活时拒绝任意选路。
- 辅助 CLI 调用直接交回原 CLI；CUA 自用的辅助 App Server 不属于重复聊天后台。
- App Server 断线会结束适配器，不做聊天请求自动重放。工具代理可以在后续请求重连；断线时的在途调用返回结果未知。CUA 内存变量不保证恢复。
- 后台读取原 Codex profile，再使用命令行覆盖两项工具的传输配置；保留原插件工具列表和审批策略。Desktop 的其他临时 CLI 覆盖没有全部迁移，不能据此保证所有 App 专属功能。
- 日志记录进程和连接事件，不主动记录聊天正文、原生身份或工具返回值；`daemon.log` 是原 CLI 自身的输出。原生插件仍执行正常身份和审批检查。
- CLI 版本在准备时限定；App/插件升级后需重新准备并做一次正常流程确认。登录服务已实际启用并核对环境；最终部署的停用及重新登录行为尚未专项复验。

## 本阶段验证

```bash
pnpm --dir integrations/codex-workflow build:desktop
pnpm --dir integrations/codex-workflow test:desktop
pnpm --dir integrations/codex-workflow typecheck
pnpm --dir integrations/codex-workflow test
```

针对性回归使用隔离 socket 与模拟后台，检查双向消息和审批转发、工具断线不重发、3 个并发启动只创建一份后台、辅助 CLI 转交、未知端点保护及路径恢复。它们不冒充原装 Desktop 验收。

实验版已完成冷启动 2 次、正常重开复用 1 次，均有桌面工具和 IAB 实际调用证据。工程版最终只需一次人工正常流程确认：启用 → 原图标冷启动 → 工具/IAB → 正常退出 → 原图标重开 → 原聊天工具/IAB → 停用恢复。异常恢复和压力测试后续按需要补充，不再作为当前集成门槛。
