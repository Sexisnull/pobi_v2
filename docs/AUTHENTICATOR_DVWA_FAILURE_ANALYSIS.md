# 认证 Agent 登录 DVWA 失败分析报告

> 生成日期：2026-08-16
> 目标：`122.51.72.186:8081`（DVWA，Damn Vulnerable Web Application）
> 凭据：`admin / password`（任务上下文已提供，账号密码本身正确）
> 相关 Session：`4e68baab-1cc9-430d-b475-15674f183242`
> 相关 Agent：`02c1a686-ca84-474e-bb8c-70a1d90eb5a3`

## 1. 结论

认证 Agent 用正确的 `admin/password` 仍多次登录失败，**根因不是账号密码错误，而是框架无法处理带 CSRF token 的表单登录**。

具体存在三层缺陷：

1. **工具层（根因）**：浏览器步骤模型只支持静态 `fill/select/check/click/press`（`browser.py:790-834`），无法"先从页面读取动态隐藏字段（如 DVWA 的 `user_token`），再回填到提交表单"。结果登录 POST 永远缺少有效的 CSRF token，被服务端拒绝。
2. **提示词层**：提示词让 Agent 在发现默认口令时倾向误选 `auth_flow="http"`（HTTP Basic），而 DVWA 是表单登录、根本不认 Basic Auth（落盘的 `default.json` 里就是 `Authorization: Basic admin:password` 这种错误方式）。
3. **验证层**：登录"成功"判定过松。无显式成功信号时工具直接兜底 `success=true`，并把 `metadata.validated=true` 写入 `AuthContext`，造成"认证成功"的假象。

## 2. 证据

### 2.1 实测对照实验（HTTP 复现）

用 curl 直接对目标做两组对照，精确复现 Agent 的失败路径与正确路径的差异：

**实验 A —— 不带 user_token（复现 Agent 的失败行为）**

```
POST username=admin&password=password&Login=Login  (无 user_token)
→ HTTP/1.1 302 Found
→ Location: login.php
用返回的 cookie 打 index.php → 302 弹回 login.php
```

表现：拿到的是未认证会话 `PHPSESSID`，`index.php` 始终被弹回登录页。

**实验 B —— 先抓 user_token 再提交（正确做法）**

```
GET  /login.php  → 取出 user_token' value='5440d805763ba8d20ccfd63be73efef4'
POST username=admin&password=password&user_token=5440d805763ba8d20ccfd63be73efef4&Login=Login
→ HTTP/1.1 302 Found
→ Location: index.php
用返回的 cookie 打 index.php → HTTP 200
```

**对照表**

| 实验 | 操作 | `Location` / `index.php` | 结果 |
|---|---|---|---|
| A（复现 Agent） | 无 `user_token` 提交 | `login.php` / 302 弹回 | ❌ 失败 |
| B（正确做法） | 带 `user_token` 提交 | `index.php` / HTTP 200 | ✅ 成功 |

唯一差异就是一个 `user_token` 字段。账号密码 `admin/password` 本身**完全正确**。

### 2.2 存储的认证上下文（落盘文件）

路径：`~/.pobi_v2/agents/02c1a686-.../4e68baab-.../auth_context/`

**`default.json`**

```json
{
  "headers": {"Authorization": "Basic YWRtaW46cGFzc3dvcmQ="},  // 解码 = admin:password
  "cookies": [],
  "browser_storage": {"localStorage": {}, "sessionStorage": {}},
  "metadata": {"auth_flow": "http", "auth_type": "api_key",
               "validated": true, "response_status": 200}
}
```

- `Authorization: Basic admin:password` → 目标 DVWA 不认 Basic Auth（实验已证）。
- `auth_type` 被误标为 `api_key`（实为 basic）。
- `validated: true` 是误判（见 2.3）。

**`playwright_state.json`**

```json
{"cookies": [
  {"name": "PHPSESSID", "value": "muluu2vmhrciq5lmc67fi7vta4"},
  {"name": "security", "value": "low"}
]}
```

- 这是浏览器访问登录页拿到的**未认证会话 cookie**（`security=low` 是 DVWA 安全等级）。
- 与实验 A 现象一致：开了浏览器但无 token 提交，登录未成功。

**文件割裂**：`default.json` 的 `cookies: []`，把浏览器侧真实抓到的 `PHPSESSID`/`security` 丢弃了；下游若只读 `default.json` 拿不到任何 cookie。

### 2.3 验证层误判

- Basic 流程 `authenticate.py:660-684` 仅做一次 `probed` 连通探测拿 200 即写 `validated:true`，未验证凭据真能登录。
- Browser 流程 `wait_for_auth_success` 在 Agent 未配置 `success_url_contains`/`success_selector` 时（`authenticate.py:308-316`），依赖"未抛异常"兜底 `success=true`（`authenticate.description.jinja2:251`）。
- 结果：真实登录从未成功（`default.json` 之前），`AuthContext` 却被标 `success=true / validated=true` 落盘。

### 2.4 DVWA token 字段格式细节

DVWA 的 `user_token` 字段使用**单引号**：

```html
<input type='hidden' name='user_token' value='5440d805763ba8d20ccfd63be73efef4' />
```

解析 HTML 时必须兼容单引号，否则会像初次正则 `name="..."`（双引号）那样抓取为空，导致即便加了"抓 token"能力仍会失败。

## 3. 失败链路

```
用户给定 admin/password
  ↓
提示词"默认口令→Basic 流程" → Agent 可能误选 auth_flow="http"
  ↓  (或误打误撞走 form，但)
  ↓
form 流程 steps 只能静态填 username/password，无法抓 DVWA 的 user_token(CSRF)
  ↓
DVWA 校验 user_token 失败 → 302 回 login.php（登录未成功）
  ↓
工具无显式成功信号 → 兜底 success=true → AuthContext 误标 validated=true 落盘
  ↓
下游复用"假成功" profile → 实打目标全被弹回登录页
```

## 4. 修复建议（按性价比排序）

1. **工具层补 CSRF 能力（根因）**：给 `BrowserStep`（`browser.py`）增加 `extract` / `fill_from_page` 步骤类型，支持"从选择器提取文本/value 写入 context 的某 key"，让登录步骤能先抓 `input[name=user_token]` 再回填。这是让表单登录类目标（DVWA、WordPress、多数 PHP 应用）真正可用的必需能力。HTML 解析需兼容单引号属性。
2. **提示词层纠正误导**：`authenticator.instructions.jinja2` 的"默认口令"示例不应暗示走 Basic，应明确"有登录表单就用 `form`，Basic 仅用于无表单的 HTTP Basic 保护端点"；并增加 CSRF 专项指引。
3. **验证层收紧**：`_persist_and_summarise` 把 `success=true` 写死（`authenticate.py:190`）是危险的。应让成功信号缺失时**不写 `validated=true`**，而是标 `validated=false`/warn，避免假阳性落盘。

## 5. 涉及源码位置速查

| 关注点 | 位置 |
|---|---|
| Agent 定义 | `pobi_agent/agents/generic_agents/authenticator_agent.py` |
| 认证工具调度 | `pobi_agent/tools/browser/authenticate.py`（service:692 / form:369 / json:559 / http_basic:678 / persist:173） |
| 浏览器步骤模型 | `pobi_agent/tools/browser/browser.py:790-834` |
| AuthContext 结构 | `pobi_agent/auth_resolver/auth_resolver.py:130-156` |
| AuthContext 构造 | `pobi_agent/auth_resolver/auth_context_utils.py:83-133, 414-479` |
| 持久化路径 | `pobi_agent/auth_resolver/auth_resolver.py:264-311`（基于 `constants.py:33,40` 的 `DEADEND_AGENTS_PATH`） |
| 提示词 | `pobi_prompts/authenticator.instructions.jinja2`、 `pobi_prompts/tools/authenticate.description.jinja2` |
