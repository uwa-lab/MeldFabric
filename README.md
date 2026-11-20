# MeldFabric

MeldFabric 是一个基于 Strands Agents 的配置驱动 Agent 服务库。它把 Agent、模型、工具、会话记忆、编排和发布端点统一到一个 FastAPI 服务与可视化管理后台中。

## 安装

主包已包含 Strands、FastAPI、管理后台、MCP、A2A，以及 SQLite、MySQL、PostgreSQL 支持：

```bash
pip install meldfabric
```

可选能力：

```bash
pip install "meldfabric[langfuse]"
pip install "meldfabric[sandbox-docker]"
pip install "meldfabric[all]"
```

## 五分钟启动

创建 `app.py`：

```python
from typing import Annotated

from pydantic import Field

from meldfabric import AgentKit, register


@register.tool(id="orders.status", description="查询订单状态")
def order_status(
    order_id: Annotated[str, Field(description="订单编号")],
) -> dict[str, str]:
    return {"order_id": order_id, "status": "paid"}


kit = AgentKit()
kit.run()
```

启动服务：

```bash
export MELDFABRIC_ADMIN_KEY='replace-with-a-strong-key'
python app.py
```

打开：

- 管理后台：`http://127.0.0.1:8000/admin`
- OpenAPI：`http://127.0.0.1:8000/docs`
- 运行诊断：`http://127.0.0.1:8000/ready`

如果没有传入控制数据库，MeldFabric 会创建 `.meldfabric/control.db`。如果首次启动也没有提供管理员 Key，服务会随机生成一个并在初始化日志中显示一次。

进入后台后，按以下顺序完成首个端点：

1. 在“模型”中创建服务商、发现并注册模型；
1. 在“工具”中确认代码工具已经出现；
1. 创建 Agent，选择模型并挂载工具；
1. 创建发布端点并生成访问 Key；
1. 保存并应用最新配置，然后在“端点测试”中对话。

## 调用 AG-UI

使用发布端点生成的访问 Key，而不是管理员 Key：

```bash
curl -N -sS -X POST 'http://127.0.0.1:8000/api/ag-ui' \
  -H 'Authorization: Bearer <agent-api-key>' \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -H 'X-User-ID: user-001' \
  -d '{
    "threadId": "conversation-001",
    "runId": "run-001",
    "messages": [
      {"id": "message-001", "role": "user", "content": "查询订单 A-100"}
    ]
  }'
```

正常响应包含 `RUN_STARTED`、文本或工具事件，以及 `RUN_FINISHED`。

## 代码注册

MeldFabric 使用统一的 `register` 入口：

- `@register.tool`：工具；
- `@register.plugin`：插件；
- `@register.hook` / `@register.hook_group`：生命周期 Hook；
- `@register.intervention` / `@register.intervention_group`：执行干预；
- `@register.graph_condition`：Graph 条件。

装饰器只有在 Python 导入并执行其所在模块后才会注册。请在创建 `AgentKit` 前导入能力模块；不需要配置扫描目录或 `tool_modules`。

所有参数和返回值都必须有受支持的类型注解，并能生成合法 JSON Schema。可以使用 `Annotated[T, Field(description="...")]` 描述参数。生产代码建议显式设置稳定的 `id`；修改自动生成 ID 所依赖的模块路径或函数名，会被视为删除旧注册项并新增一项。

插件、Hook 和 Intervention 的完整签名及双模式示例可在管理后台“开发指南”中查看。

## 控制数据库与密钥

可以显式指定独立的控制数据库：

```python
kit = AgentKit(
    "postgresql://user:password@database.example.com/meldfabric",
    admin_key_env="MELDFABRIC_ADMIN_KEY",
)
```

控制数据库保存系统配置、配置快照、加密凭据、访问 Key 哈希和运行协调数据。Agent 的会话、长期记忆等业务数据使用后台创建的数据库资源，两者相互独立。

出站凭据由工作空间密钥加密。默认密钥文件位于 `.meldfabric/keys/<workspace_id>.key`；备份或迁移控制数据库时也必须安全迁移该文件。

配置“保存”只创建新的配置版本；点击“应用配置”后，新任务才使用它，正在执行的任务继续使用启动时的配置。

## 本地开发

```bash
uv sync --dev
npm --prefix web ci
npm --prefix web run build
uv run pre-commit install
uv run pre-commit run --all-files
uv run pytest
```

构建 wheel 和 sdist：

```bash
bash scripts/build-package.sh
```

脚本会先构建 Vue 管理后台，再执行 Python 打包。不要直接用 `uv build` 代替完整构建。

## 发布

每次 push 和 pull request 都会运行质量流水线。发布流水线从受保护分支手动触发：

- `dev` 发布 beta；
- `main` 发布正式版本。

提交信息遵循 Conventional Commits。`update:` 触发 minor，`feat:`、`fix:`、`perf:` 触发 patch；其余允许的类型不单独提升版本。发布流水线生成版本、构建前端与 Python 发行包，并将同一份产物上传到 PyPI 和 GitHub Release。
