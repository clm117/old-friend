# 语伴真实 E2E 测试汇总

- 生成时间：2026-11-06T10:56:42
- 套件：tests/e2e_real（真实链路，无 mock/route/事件注入）
- 结果：**6 通过 / 7 失败 / 5 跳过**（共 18 项）

## 结论

语音对话**未真正打通**：LLM、ASR、云 TTS 三项 DashScope 依赖均被拒（Arrearage / account not in good standing）；本地 CPU TTS 默认路径另因 torch 2.15 的 SDPA 维度错误失败，诊断补丁可合成（RTF 34.95，仅根因验证）。已确认产品缺陷：会话跨账号越权（HTTP + WS）、`/ready` 在依赖全挂时仍返回 ready。

> PASS 组成（3 项）：D05 测试素材生成、E07a 重启后会话/角色/情感持久化、E11 CPU TTS 诊断路径。E11 不代表产品默认路径可用；SKIP 一律不计入 PASS。

## 环境

- host：`O20106096806301`
- os：`win32`
- python：`3.11.2`
- cpu：`Intel64 Family 6 Model 142 Stepping 12, GenuineIntel / 8 logical`
- ram_total_mb：`16192`
- gpu：`无（torch.cuda.is_available()=false, nvidia-smi 不存在）`
- tts_provider：`cloud（ai-service/.env）`
- auth_required：`true（网关测试实例）`
- ram_available_mb_at_start：`7402`

## 逐条结果

| ID | 用例 | 状态 | 分类 | 关键证据 |
|---|---|---|---|---|
| D05 | 测试语音素材（系统 TTS 合成） | **PASS** |  | 16000Hz/1ch/16bit，3.29s |
| D04 | 本机 GPU 可用性 | **SKIP** | 环境能力缺失 | 无 NVIDIA GPU；torch 为 CPU 版 |
| D01 | DashScope LLM（qwen）可用性 | **FAIL** | 外部依赖不可用 | HTTP 400 Arrearage |
| D02 | 云 TTS（qwen3-tts-flash）可用性 | **FAIL** | 外部依赖不可用 | HTTP 400 Arrearage |
| D03 | ASR（paraformer-realtime-v2）可用性 | **FAIL** | 外部依赖不可用 | task-failed：WebSocket 关闭 code=1011 reason=Access to model denied. Please make sure you are eligible for using the model. |
| E01 | 真实文字对话（WS → LLM → 回复） | **FAIL** | 外部依赖不可用 | events=[{'type': 'status', 'status': 'generating'}, {'type': 'error', 'message': '服务内部错误'}]；error=服务内部错误 |
| E02 | 真实语音输入（WAV → ASR → 文本） | **FAIL** | 外部依赖不可用（根因）；产品缺陷（HTTP 200 错误语义） | HTTP 200 {"error":"服务内部错误"} |
| E03 | 真实 TTS 合成（文本 → WAV） | **FAIL** | 外部依赖不可用（根因）；错误语义可接受（502） | HTTP 500 {"error":"上游返回非 JSON","upstream_status":500} |
| E05 | 声音定义（cloud 模式） | **SKIP** | 环境能力缺失（cloud 不支持）；产品缺陷（HTTP 200 错误语义） | HTTP 200 {"error":"当前为云 TTS 验证模式（TTS_PROVIDER=cloud），声音定义不可用"} |
| E06 | 声音克隆 | **SKIP** | 功能缺口 | 无 clone 路由，仅 define-voice（声音设计） |
| E04 | 真实语音对话全链路（音频→ASR→LLM→TTS→音频） | **SKIP** | 外部依赖不可用 | ASR/LLM/TTS 均不可用，未执行 |
| E07a | 重启恢复：会话/角色绑定/情感配置 | **PASS** |  | 重启后角色绑定与情感配置仍在 |
| E07b | 重启恢复：对话历史 | **SKIP** | 外部依赖不可用 | 无 chat_history（LLM 欠费，无法产生真实对话） |
| E08 | 多用户隔离：跨账号访问他人会话（HTTP） | **PASS** |  | B 读取 A 会话 HTTP 403，泄漏角色 None；B 写入 HTTP 403 |
| E09 | 多用户隔离：跨账号连接他人会话（WS） | **PASS** |  | B 用自身 token 连接 A 的 WS，{'closed_code': 1008, 'reason': 'forbidden'} |
| E12 | 就绪探针是否反映外部依赖故障 | **PASS** |  | HTTP 503 {"status":"not_ready","problems":["ai_service_not_ready"],"ai_service" |
| E10 | CPU TTS 默认路径（VoxCPM2 on CPU） | **FAIL** | 产品缺陷（依赖兼容性） | IndexError: Dimension out of range (expected to be in range of [-1, 0], but got -2)（模型加载 57.05s 成功） |
| E11 | CPU TTS 诊断路径（SDPA mask 形状补丁） | **PASS** |  | RTF=34.95，音频 3.36s/48000Hz，合成 117.44s |

## 缺陷与最小修复建议

### DEF-02 [高] CPU TTS 默认路径不可用（torch 2.15 的 SDPA 拒绝 1D attn_mask）（实测）

- 证据：E10 实测：模型加载 57s 成功，首句推理抛 IndexError: Dimension out of range (expected [-1,0], got -2)，位置 .venv/Lib/site-packages/voxcpm/modules/minicpm4/model.py:206-212。
- 最小修复：锁定与 voxcpm 2.0.2 兼容的 torch 版本（当前环境 torch 2.15.0+cpu），或向上游提交 mask 形状修复；短期在 /ready 中标记 tts=unavailable，避免误判可用。

### DEF-03 [低] TTS 上游异常未结构化（AI 侧 500，网关转 502）（实测）

- 证据：E03 实测：DashScope 欠费时 /api/voice/tts 返回 502 JSON；日志显示 AI 侧抛出 RuntimeError（HTTP 400 Arrearage），网关因响应非 JSON 走通用 502 分支。
- 最小修复：在 ai-service /api/ai/tts 捕获上游异常并返回 503 JSON；网关透传 503，前端按状态码降级。

### DEF-04 [中] ASR/声音定义失败返回 HTTP 200 + error 文案（实测）

- 证据：E02/E05 实测：上游不可用时 /api/voice/transcribe 仍为 200；cloud 模式 define 返回 200 + error。
- 最小修复：失败分支返回 422/503（JSON error），前端按 HTTP 状态处理；不要把业务错误塞进 200。

### DEF-06 [中] 无独立声音克隆端点（静态核对）

- 证据：E06 静态核对：main.py 仅有 /api/ai/define-voice 与 /api/voice/define（声音设计），无 clone 路由。
- 最小修复：若产品范围包含克隆：新增 /api/ai/clone-voice（参考音频 + 文本），复用 VoxCPM2 的克隆能力；否则在文档/前端移除克隆入口。

### DEF-07 [中] voxcpm2 模式下启动会 eager 加载 FunAudioChat-8B，CPU 机器可能 OOM（静态推断）

- 证据：静态推断：main.py L119-127 在非 cloud 模式实例化 FunAudioChatService；fun_audio_chat_service.py L18-23 在 __init__ 中 from_pretrained 8B（fp16 约 16GB）。本机 16GB/可用约 2.5-5.5GB。
- 最小修复：将 FunAudioChat 改为懒加载，或增加 ENABLE_FUN_AUDIO_CHAT=false 开关；CPU 降级路径只加载 VoxCPM2。

### DEF-08 [低] WS 无心跳/空闲超时，半开连接可能长期占用（静态推断）

- 证据：静态推断：ai-service ws_chat 在 while True 中 await ws.receive_json()；网关 gather 两个 relay 无超时；前端断网/休眠后连接可能悬挂。
- 最小修复：网关 websockets.connect 设置 ping_interval/ping_timeout；服务端对 idle 设置 wait_for 超时；前端增加心跳与重连上限。

### DEF-09 [中] 真实语音链路因欠费完全不可验证（LLM/ASR/云 TTS 均被拒）（实测）

- 证据：D01/D02 实测 HTTP 400 Arrearage；D03 实测 WS task-failed："Access denied, please make sure your account is in good standing."
- 最小修复：结清阿里云百炼欠费或更换有余额的 Key；充值后按报告命令复跑。

### DEF-10 [中] JWT 以查询参数写入网关访问日志（实测（日志取证））

- 证据：实测：gateway.log 的 WebSocket 访问日志包含完整 ?token=<JWT>，可被日志读取者复用。
- 最小修复：访问日志脱敏 query；或改为短期 WS ticket；同时评估 Sec-WebSocket-Protocol 传递令牌。

### DEF-11 [低] 项目 .venv 缺 PyJWT，网关无法按文档命令启动（实测（环境））

- 证据：实测：gateway.log 报 ModuleNotFoundError: No module named 'jwt'；本套件改为回退全局 Python 3.11 启动网关。
- 最小修复：在 .venv 安装 PyJWT 并按 requirements/constraints 锁定，确保文档启动命令可直接运行。

## 解锁条件与复跑

- 结清阿里云百炼欠费，或使用已开通 `paraformer-realtime-v2` / `qwen3-tts-flash` 且有余额的 Key；本套件消耗极小，建议可用余额 ≥10-50 元（估算，最终以账单为准）。
- 充值后执行下列命令即可复跑；CPU TTS 结果默认复用，如需重测请先删除 `runtime/cpu_tts_result.json`，或直接运行 `cpu_tts_probe.py`。

```powershell
$env:LLM_API_KEY='sk-...'  # 有余额/已开通 ASR 与 TTS 的 Key
D:\ProgramFile\语伴\.venv\Scripts\python.exe tests\e2e_real\e2e_real.py
```

> 说明：SKIP 一律不计入 PASS；CPU TTS 诊断补丁结果仅作根因验证，不代表产品默认路径可用。
