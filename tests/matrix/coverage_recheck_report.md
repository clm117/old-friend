# 测试覆盖重检与补测报告

日期：2026-11-09
范围：GAP-05 第 1/2/3 批改动的可测面 + 本轮新增 12 条预设音色
约束：未 commit / push；未启停付费 GPU；未删除用户数据；BLOCKED 不计为 PASS

---

## 一、方法

以现有全部资产为基线：矩阵 T1/T2/T3、深度套件（API 77 + UI 87 + 自检 3）、基础/冒烟、E2E real、前端交互验收、单测（state_store）。

判定"能测"的标准：仅依赖本机可启动的 AI 服务（8084）、网关（8005）、静态前端（8899）与临时数据目录；不依赖 DashScope 余额、GPU、公网域名。逐项核对代码路径是否已有对应用例，再按以下三类归档：

- A：现在能测且未测 → 本轮补测
- B：现在能测且已覆盖 → 只说明覆盖位置
- C：仍被外部资源阻塞 → 只列解锁条件

---

## 二、A / B / C 清单

### A 类：现在能测且未测（24 项，本轮全部补测）

| # | 功能点 / 代码位置 | 为何现在能测 | 此前未覆盖原因 | 验证方式 | 用例 |
|---|---|---|---|---|---|
| A1 | 会话分页 limit=0（`ai-service/main.py` `GET /api/ai/sessions`，`Query(ge=1, le=100)`） | 本机 AI 服务即可 | 历史遗漏 | 接口 | T1-SES page_limit_zero |
| A2 | 分页 limit=101 | 同上 | 历史遗漏 | 接口 | page_limit_over |
| A3 | 分页 offset=-1 | 同上 | 历史遗漏 | 接口 | page_offset_negative |
| A4 | 分页 offset 超范围 | `state_store.list_sessions` 内部 clamp | 历史遗漏 | 接口 | page_offset_beyond |
| A5 | 会话列表排序（更新时间倒序） | 同上 | 历史遗漏 | 接口 | session_order |
| A6 | 并发重命名最终一致（`state_store` 锁） | 本机并发 | 历史遗漏 | 并发 | session_rename_concurrent |
| A7 | 删后绑定/写入 → 404（`_session_owner_error` 先查墓碑） | 本机 | 墓碑功能第 3 批才接入 | 接口 | session_delete_then_bind |
| A8 | 跨账号 WS → 1008 `forbidden`（`/ws/chat/{sid}`） | 本机两条 token | 历史遗漏 | 接口/WS | iso_ws_forbidden |
| A9 | 他人会话删除情感配置 → 403（`DELETE /api/ai/emotion-config`） | 本机两条 token | 越权矩阵未覆盖 DELETE | 越权 | iso_delete_emotion |
| A10 | CORS 预检覆盖 GET/POST/PUT/PATCH/DELETE | 网关本地 | 此前只测 GET/DELETE | 接口 | cors_preflight_methods |
| A11 | 内部签名 `X-Yuban-*` 缺失/篡改/过期/未来 → 401（`_trusted_user`） | 本机直连 AI | 此前无任何负向用例 | 故障注入 | internal_sig_invalid |
| A12 | 合法签名放行 | 同上 | 同上 | 接口 | internal_sig_valid |
| A13 | 上游拒连 → 502 统一文案（`_upstream_error`） | 隔离网关进程 | 历史遗漏 | 故障注入 | upstream_down |
| A14 | 上游挂起超时 → 502（`AI_SERVICE_TIMEOUT`） | 黑洞 socket + 隔离网关 | 历史遗漏 | 故障注入 | upstream_timeout |
| A15 | 情感配置删除后重新保存/初始化 | 本机 | 历史遗漏 | 接口 | emotion_delete_resave |
| A16 | 重启后墓碑仍生效（`state.json` 持久化） | 本机重启 AI | 墓碑功能第 3 批才接入 | 重启 | restart_tombstone |
| A17 | state.json 损坏 → 安全空态（`PersistentStateStore._load`） | 临时 state 文件 | 历史遗漏 | 单测 | test_corrupted_state_file_recovers |
| A18 | 半写临时文件 + 截断 state → 忽略并恢复 | 同上 | 历史遗漏 | 单测 | test_truncated_state_and_stale_tmp_ignored |
| A19 | 多会话并发写 + reload 一致 | 同上 | 历史遗漏 | 并发单测 | test_concurrent_writes_and_reload |
| A20 | 墓碑/会话上限裁剪与索引一致（`_MAX_SESSIONS/_MAX_TOMBSTONES`） | 白盒单测 | 历史遗漏 | 单测 | test_prune_caps_and_index_consistent |
| A21 | 历史会话首写认领 + 索引重建 | 同上 | 历史遗漏 | 单测 | test_claim_legacy_and_index_rebuild |
| A22 | Compose 静态可执行性（YAML/服务/健康检查/端口冲突） | 无 Docker 也可静态校验 | 历史遗漏 | 静态校验 | ci_smoke.check_compose |
| A23 | 启动门控（空账号表 / 默认 JWT_SECRET → 退出） | 子进程 import 网关 | 历史遗漏 | 启动校验 | ci_smoke.check_startup_guards |
| A24 | 服务端删除会话：刷新收敛 + 页内删除后发送 1008 自动重建 | 真实前后端链路 | 历史遗漏 | 前端交互 | 深度 B86 |

### 本轮新增需求（12 条预设音色，不属于覆盖缺口，单独计量）

| # | 用例 | 结果 |
|---|---|---|
| N1 | 12 条目录字段齐全、顺序一致、ready=false、notice 明确 | PASS |
| N2 | 12 条均可按 id 生成含语感特征的 voice_prompt；相声评书 style=false + 备注 | PASS |
| N3 | 未知预设 → 404（取 prompt / 绑定） | PASS |
| N4 | 绑定会话：voice_id=null、ready=false、notice 含"未就绪"、落盘一致 | PASS |
| N5 | 跨账号绑定预设 → 403 | PASS |
| N6 | 前端列出 12 条、选中展示 prompt 与未就绪提示、可应用到会话 | PASS（B87） |

### B 类：现在能测且已覆盖（不重复）

| 功能 | 覆盖位置 |
|---|---|
| 标签 → voice_prompt 20 条逐字基线 | 矩阵 T1-VOX voice_prompt ×20 |
| 默认回退（空标签→性别默认） | T1-VOX default_*、speed_boundary |
| 会话创建/列表/删除/归属（GET/DELETE） | T1-SES 既有 390 条 |
| 角色 CRUD、绑定/解绑、名称冲突、越权 | T1-CHA、深度 API A35-A50 |
| 情感配置六维边界、CRUD、越权读 | T1-EMO、深度 A31-A34 |
| 限流分桶（登录/普通/昂贵） | 深度 A57/A58/A59 |
| CORS 白名单/非白名单/401 带 CORS 头 | 深度 A60-A62、B60 系列 |
| token 篡改/过期/算法混淆 | 深度 A52-A56、B71 |
| 上传大小与魔数校验 | 深度 A51/A72-A76 |
| 前端 7 标签、表单、边界、XSS、损坏 localStorage | 深度 B01-B85 |
| 基础 UI 冒烟 | ui_test_suite 32 项 |

### C 类：仍被外部资源阻塞（只列解锁条件，不计入本轮）

| 项 | 数量 | 解锁条件 |
|---|---|---|
| 真实多轮对话（T2-DIA） | 54 | DashScope 余额恢复 + LLM/TTS 可用 |
| 本机 GPU / 本地 VoxCPM2 / 云 TTS / ASR / 声音定义 / 情感音色 / 回放（T3-PRB） | 8 | GPU 或云配额；声音克隆还需产品端点 |
| 方言未就绪回退的真实合成（T1-VOX-170） | 1 | 云方言 voice_id 配额 |
| 儿童/幼儿音色（T1-POP-143/144） | 2（能力缺失） | 产品接入童声模型 |
| 方言/童声/老年/情感音色、声音克隆端点、角色解绑端点（T1-VOX-165/166、T1-SES-259） | 3（能力缺失） | 产品补齐端点/模型 |

**"能测但未测"清零**：本轮前 A 类 24 项；本轮后 A 类 0 项。

---

## 三、补测执行结果

### 1. 矩阵 T1（含 21 条本轮新增用例）

```
计划 411 / 执行 411 / 通过 405 / 失败 0 / 能力缺失 5 / 阻塞 1
```

- 新增 16 条覆盖用例 + 5 条预设音色用例全部 PASS。
- 5 条能力缺失与 1 条阻塞见上表 C 类，均未记 PASS。

### 2. 深度套件

```
总计 167 / 通过 167 / 失败 0 / 阻塞 0
（API 77、UI 87、自检 3）
```

### 3. 基础/冒烟

```
基础 UI：33 项 → 通过 32 / 失败 0 / 阻塞 1（无麦克风，停止按钮分支不可达）
ci_smoke：15 / 15 PASS（含 Compose 静态校验、启动门控）
state_store 单测：8 / 8 PASS
```

### 4. 合并门禁数字

| 套件 | 通过 | 失败 | 阻塞/缺失 |
|---|---|---|---|
| 矩阵 T1 | 405 | 0 | 6（5 能力缺失 + 1 阻塞）|
| 矩阵 T2 | 0 | 0 | 54 阻塞 |
| 矩阵 T3 | 0 | 0 | 8 阻塞 |
| 深度套件 | 167 | 0 | 0 |
| 基础 UI | 32 | 0 | 1 阻塞 |
| ci_smoke | 15 | 0 | 0 |
| state_store | 8 | 0 | 0 |
| **合计** | **627** | **0** | **69** |

---

## 四、缺陷清单与修复状态

本轮未发现产品功能缺陷；发现并修复 3 个**测试资产缺陷**：

| # | 缺陷 | 影响面 | 修复 |
|---|---|---|---|
| D1 | 内部签名"未来时间戳"用例用 `now+301`，恰落在 300s TTL 边界外沿，偶发被放行 | T1-SYS 签名负向用例误报 | 改为 `now+3600`，稳定 401 |
| D2 | 隔离网关探活客户端超时 3s，与网关 `/health` 内部 3s 上游超时重叠，黑洞场景误判启动失败 | T1-SYS upstream_timeout 误报 | 探活超时放宽到 10s，并增加端口重试与 stderr 证据 |
| D3 | B87 复用 B86 遗留的"已服务端删除"会话，预设绑定走 404 分支导致状态条无文案 | 深度 UI 用例误报 | B87 开头清除遗留 sessionId 并 reload，与 B86 解耦 |

修复后已重跑对应项及全量门禁，以上数字为修复后结果。

---

## 五、本轮代码改动（工作区，未提交）

- `ai-service/voice_catalog.py`：新增 12 条预设（id/名称/性别/年龄段/语感/locale/voice_type/fallback/capabilities/voice_id/notice），`preset_list/preset_status/preset_prompt/preset_available`；`get_catalog` 暴露 `voice_presets`、`preset_count`、`capabilities.voice_presets/preset_voice_ready`。
- `ai-service/main.py`：`GET /api/ai/voice-presets`、`GET /api/ai/voice-presets/{id}/prompt`、`POST /api/ai/sessions/{sid}/voice-preset`；`_resolve_session_style` 预设优先；`_resolve_session_voice` 未绑定时回退默认音色并显式提示。
- `api-gateway/app.py`：对应三条网关路由与参数校验。
- `frontend/index.html`、`frontend/script.js`：角色页新增预设音色下拉、voice_prompt 展示、未就绪提示、应用到当前会话。
- 测试资产：`tests/matrix/generate_matrix.py`、`tests/matrix/matrix_cases_c.py`、`tests/matrix/run_matrix.py`、`tests/test_state_store.py`、`tests/ci_smoke.py`、`ui_test_deep.py`。
