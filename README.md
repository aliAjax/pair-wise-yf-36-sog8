# 生物样本库知情同意与撤回

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8302`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8302
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `participant`：参与者；`consent`：同意版本；`sample`：样本；`withdrawal`：撤回申请。

## 撤回级联

- 撤回申请`approve`后，申请中列出的样本立即标记：已借出(`on_loan`)转`pending_recall`(待召回)，在库(`stored`)转`pending_disposal`(待处置)。
- `pending_recall`样本归还(`return`)时转为`pending_disposal`，不再回到`stored`。
- 参与者存在已批准或已执行的撤回时，其样本的`loan`和`anonymize`会被拒绝，防止绕过撤回结果；从`pending_disposal`发起的`anonymize`/`destroy`属于处置路径，仍然允许。
- 撤回申请`execute`时会合并申请中漏填的样本（该参与者名下所有在库/已借出样本），去重后逐份处置，保证一份样本只处置一次；处置结果写入撤回单的`data.disposal`。
- 对同一撤回申请重复`execute`是幂等重放：直接返回首次执行记录的处置结果，不会重复处置。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

样本销毁和外部机构调用是流程演示，不会自动删除真实存储中的样本。
