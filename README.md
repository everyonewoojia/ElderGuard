# 银发守护 - 空巢老人智能看护系统

## 项目简介

银发守护是面向空巢、独居老人的智能看护辅助工具，构建**感知→理解→规划→执行→反馈→记忆**完整服务闭环。

**系统定位**：辅助看护工具，不替代医疗诊断，不提供医疗救治。

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                        家属端                               │
│                    微信小程序                                │
└─────────────────────────────────────────────────────────────┘
                              │
                              │ REST/WebSocket
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      云端中枢                               │
│                     FastAPI                                 │
│  ┌─────────────┬─────────────┬─────────────┬─────────────┐  │
│  │   接入层    │  应用服务层 │  Agent层    │  AI能力层   │  │
│  └─────────────┴─────────────┴─────────────┴─────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              │ 安全通道
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      老人端                                 │
│               Python + Vosk (本地ASR)                       │
│  ┌─────────────┬─────────────┬─────────────┬─────────────┐  │
│  │  语音采集   │  事件总线   │  记忆管理   │  业务插件   │  │
│  └─────────────┴─────────────┴─────────────┴─────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## 技术栈

| 组件 | 技术选型 | 说明 |
|------|---------|------|
| 老人端 | Python + Vosk | 离线语音识别 |
| 云端 ASR | 阿里云实时语音识别 | 云端语音转文字 |
| 云端 TTS | 阿里云智能语音合成 | 文字转语音 |
| 大模型 | 通义千问 qwen-turbo | 意图理解、对话生成 |
| 短信推送 | 阿里云短信服务 | 告警通知家属 |
| 云端框架 | FastAPI | REST/WebSocket |
| 数据库 | SQLite | 轻量部署 |
| 家属端 | 微信小程序 | 无需独立安装 |

## 项目结构

```
ElderGuard/
├── elder-agent/              # 老人端 Agent
│   └── src/
│       ├── core/             # 核心循环、事件总线、记忆
│       ├── plugins/          # 业务插件（提醒/告警/指令）
│       ├── ai/               # AI能力（ASR/TTS/意图）
│       └── utils/
├── cloud-server/             # 云端 FastAPI
│   └── app/
│       ├── routers/          # API路由
│       ├── models/           # 数据模型
│       ├── services/         # 业务逻辑
│       ├── core/             # 配置、数据库
│       ├── plugins/          # Agent插件
│       └── ai/               # AI能力
├── family-miniapp/           # 微信小程序
│   ├── pages/                # 页面
│   ├── components/           # 组件
│   └── utils/                # 工具函数
├── docs/                     # 文档
├── reference/                # 参考文档
└── AGENTS.md                 # 开发指南
```

## 快速开始

### 1. 环境准备

```bash
# Python 3.8+
# Node.js 14+（小程序开发）
# 微信开发者工具
```

### 2. 配置环境变量

```bash
# 云端
cd cloud-server
cp .env.example .env
# 编辑 .env 填入 API Key

# 老人端
cd elder-agent
cp .env.example .env
# 编辑 .env 填入配置
```

### 3. 安装依赖

```bash
# 云端
cd cloud-server
pip install -r requirements.txt

# 老人端
cd elder-agent
pip install -r requirements.txt
```

### 4. 启动服务

```bash
# 云端
cd cloud-server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 老人端
cd elder-agent
python src/main.py

# 小程序
# 使用微信开发者工具打开 family-miniapp 目录
```

## 测试运行（TDD）

项目采用测试驱动开发，任何功能必须先写失败测试再实现。P0 工程基线已搭建三端测试基座。

### 云端（cloud-server）

```bash
cd cloud-server
pip install -r requirements.txt
pytest                                   # 全部测试
pytest --cov=app --cov-report=term-missing   # 覆盖率
```

云端测试数据库隔离（P0-12，2026-10-17）：测试基座在导入应用前为每个
pytest 进程创建独立临时 SQLite 文件，覆盖外部 `DATABASE_URL`；若应用已提前
导入则拒绝继续。配置、模块级 engine、SessionLocal、REST 依赖及 WebSocket
会话工厂须通过身份检查；建表和清表统一由每测试一次的受保护夹具完成。
备份只复制本轮数据库，并登记在本轮临时目录中。

本次实际验证命令（在 `cloud-server` 目录，使用项目现有 `.venv`）：

```powershell
../.venv/Scripts/python.exe -B -m pytest tests/test_database_isolation.py -v -p no:cacheprovider
../.venv/Scripts/python.exe -B -m pytest tests/test_database_isolation.py tests/test_edge_integration.py tests/test_p10_quality.py -v -p no:cacheprovider
../.venv/Scripts/python.exe -B -m pytest -v -p no:cacheprovider
```

本次验证结果：

| 验证范围 | 通过 | 失败 | 错误 | 跳过 | 耗时 |
|------|------|------|------|------|------|
| 数据库隔离专项 | 35 | 0 | 0 | 0 | 7.51 秒 |
| 隔离、端云集成、备份质量三个文件兼容性回归 | 44 | 0 | 0 | 0 | 54.73 秒 |
| 云端全量 pytest | 133 | 0 | 0 | 0 | 73.46 秒 |

三次验证存在包含关系：35 项包含在 44 项中，44 项包含在 133 项中，不能相加作为总测试数。
每次运行唯一警告均为既有的 Starlette/httpx 弃用提示。本轮未验证老人端、小程序或覆盖率，
云端测试通过不代表整个项目所有模块均已验收。

隔离专项覆盖错误目标拒绝、REST/WS/直接 Session 同库、测试间隔离、备份、
正常退出和可处理异常后的清理，以及外层沙箱身份和重命名竞态检查。
拒绝清理测试在保留目标并完成断言后，由独立核验身份的框架所有者清理外层沙箱。
强制杀进程不保证清理；无法确认资源身份或删除失败时保留临时资源并报告，
不会扩大清理范围。Windows 下身份检查和受控重命名仍未彻底消除并发替换竞态，
递归删除也不具备事务式回滚能力。本轮全量测试前后系统临时目录均为 143 个，
无新增残留，也未删除历史目录。

### 老人端（elder-agent）

```bash
cd elder-agent
pip install -r requirements.txt
pytest                                   # 全部测试
pytest --cov=src --cov-report=term-missing   # 覆盖率
```

### 家属端小程序（family-miniapp，utils 逻辑测试）

```bash
cd family-miniapp
node --test tests/api.test.js tests/pages.test.js   # node v24 不支持 --test 目录形式，须指定文件
```

> 测试文件：云端 `cloud-server/tests/`、老人端 `elder-agent/tests/`、小程序 `family-miniapp/tests/`。
> 验收口径见 [AGENTS.md](./AGENTS.md)「测试规范」与「开发实施计划」。
> 本次云端验证使用项目内 `.venv/Scripts/python.exe`，已安装现有云端依赖。

## 质量验收（P10 历史记录与 P0-12 云端回归）

2026-10-17 的 P0-12 云端回归结果与 2026-10-16 的 P10 历史记录区分如下。
历史测试数及覆盖率不代表本轮重新验证结果；本轮只验证了云端测试套件。

| 端 | 测试数 | 覆盖率 | 说明 |
|------|--------|--------|------|
| 云端 cloud-server | **133/133（本轮）** | 本轮未测；历史 97% | 0 失败、0 错误、0 跳过；耗时 73.46 秒 |
| 老人端 elder-agent | 历史 88/88；本轮未验证 | 历史 86%；本轮未测 | 微内核/插件/AI门面/隐私/陪伴/视觉/通话 |
| 小程序 family-miniapp | 历史 12/12；本轮未验证 | — | mock API 逻辑测试（无自动化环境） |

P10 历史记录列出的关键质量场景（对照需求 §7.3/§7.4）如下。
本次云端回归通过不等同于重新完成这些场景的三端、真实环境或长时间运行验收：

- **AQ01** 越权访问 0 次（行级隔离 + 鉴权依赖全覆盖）
- **AQ02 / CON-B-04** 未授权采集 0 次（PrivacyGuard 双校验）
- **AQ04** 并发数据完整性（写入无覆盖/无丢失）
- **KK01** 高危告警 ≤10s 送达、重试 ≤3 次、不丢失
- **KK02** 7×24 持续待机（连续多轮分发/调度无事件丢失）
- **KK03** 误告警取消后不重复推送
- **KY01** 断网离线降级 → 恢复后补发同步
- **KY02** 数据库备份与恢复
- **XN01** 普通请求响应 ≤5s（TestClient 集成验证）
- **XN02** ≥100 家庭并发接入 + 行级隔离（无越权泄漏）
- **CON-B-01 / CON-L-06** 无医疗诊疗建议（话术不含诊疗结论，仅通知家属/急救导向）

### 模拟边界说明（答辩口径）

| 项 | 边界 |
|----|------|
| AI 能力（ASR/TTS/意图） | 调用外部 API，本地测试用规则/兜底；云端失败回退本地话术，不崩溃 |
| 视觉行为识别 / 跌倒通话 | 模拟实现（无摄像头）；疑似跌倒 10 秒无应答进入告警流程 |
| 断网离线缓存 | 内存缓存 + 重试队列（原型），重启后不持久化磁盘 |
| 小程序测试 | mock API 逻辑测试（Node），未接入 miniprogram-automator |
| 外部 API Key | 需配置阿里云/通义千问 Key（见 `.env`），未配置时功能自动降级 |

## 答辩要点（P0–P10）

- 前后端分离三子工程（老人端/云端/家属端），边界清晰、互不直接耦合
- TDD 红→绿→重构，每个功能先写失败测试再实现
- 微内核插件架构：新增看护技能不修改内核代码（XG01）
- 硬约束全部满足：越权 0 次、未授权采集 0 次、无医疗诊疗建议
- 端云闭环可演示：告警上报→云端推送→家属查看；任务/设置/授权联动
- 完整答辩材料见 `docs/答辩交付材料.docx`、演示脚本见 `docs/答辩演示脚本.md`

## API 接口

| 接口 | 方法 | 说明 | 鉴权 |
|------|------|------|------|
| /api/v1/auth/register | POST | 家属注册（bcrypt 哈希） | 否 |
| /api/v1/auth/login | POST | 家属登录（OAuth2 表单） | 否 |
| /api/v1/elder | GET | 列出当前家属绑定的老人 | 是 |
| /api/v1/elder/{id} | GET | 查询老人信息 | 是 |
| /api/v1/elder/{id}/logs | GET | 查询监护日志 | 是 |
| /api/v1/tasks | POST/GET | 创建/查询提醒任务 | 是 |
| /api/v1/tasks/{id} | DELETE | 删除提醒任务 | 是 |
| /api/v1/alerts | GET | 查询告警记录 | 是 |
| /api/v1/alerts/{id}/cancel | POST | 取消误告警（不重复推送） | 是 |
| /api/v1/privacy/auth | POST | 修改隐私授权 | 是 |
| /api/v1/edge/alerts | POST | 老人端上报告警 | 边缘 |
| /api/v1/edge/tasks | GET | 老人端拉取激活任务 | 边缘 |
| /api/v1/edge/settings | GET | 老人端拉取设置 | 边缘 |
| /api/v1/edge/privacy | GET | 老人端拉取授权状态 | 边缘 |
| /api/v1/ws/alerts?token= | WS | 家属实时告警推送（补推 pending） | 是 |

## 功能模块

### P0 核心功能

- 用药作息提醒
- 老人主动语音求助
- 家属告警推送
- 家属查看与配置提醒
- 隐私授权开关
- 久坐计时提醒
- 基础语音交互

### P1 扩展功能

- 视觉跌倒识别
- 告警消息重试
- 离线降级
- 主动情感陪伴

### P2 预留接口

- 毫米波雷达/穿戴设备
- 社区后台对接
- 高级情绪识别

## 开发规范

详见 [AGENTS.md](./AGENTS.md)

## 许可证

本项目仅供学习交流使用。
