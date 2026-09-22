# AGENTS.md - 银发守护项目开发指南

## 项目概述

**项目名称**：空巢老人智能看护 —— 银发守护多模态陪护 Agent

**项目定位**：面向空巢、独居老人的智能看护辅助工具，不替代医疗诊断，不提供医疗救治。

**系统组成**：
- 老人端 AI Agent（Python）
- 家属微信小程序
- Agent 中枢云端服务（FastAPI）

## 技术栈

| 组件 | 技术选型 | 说明 |
|------|---------|------|
| 老人端 | Python + Vosk（本地ASR） | 离线语音识别 |
| 云端 ASR | 阿里云实时语音识别 | 云端语音转文字 |
| 云端 TTS | 阿里云智能语音合成 | 文字转语音 |
| 大模型 | 通义千问 qwen-turbo | 意图理解、对话生成 |
| 短信推送 | 阿里云短信服务 | 告警通知家属 |
| 云端框架 | FastAPI (Python) | REST/WebSocket |
| 数据库 | SQLite（原型） | 轻量部署 |
| 家属端 | 微信小程序原生框架 | 无需独立安装 |

## 架构设计

### 架构风格

- **分层架构**：接入层 → 应用服务层 → Agent智能业务层 → AI能力支撑层 → 数据持久层
- **微内核架构**：Agent层采用插件模式，内核仅保留事件总线、调度器、记忆管理
- **事件驱动架构**：所有状态变化封装为标准事件对象，通过事件总线分发
- **Agent代理架构**：感知→理解→规划→执行→反馈→记忆 完整闭环

### 模块划分

#### 接入层
- M-ACC-01：老人端接入模块 (P0)
- M-ACC-02：家属小程序接入模块 (P0)
- M-ACC-03：WebSocket实时消息模块 (P1)
- M-ACC-04：第三方硬件适配模块 (P2)

#### 应用服务层
- M-APP-01：用户权限管理模块 (P0)
- M-APP-02：基础告警通知模块 (P0)
- M-APP-03：任务同步模块 (P0)
- M-APP-04：监护日志模块 (P0)
- M-APP-05：告警重试模块 (P1)
- M-APP-06：社区后台对接模块 (P2)

#### Agent智能业务层（微内核）
- M-AGT-01：事件总线 (P0)
- M-AGT-02：任务规划调度器 (P0)
- M-AGT-03：记忆管理单元 (P0)
- M-AGT-04：提醒插件 (P0)
- M-AGT-05：安全告警插件 (P0)
- M-AGT-06：指令解析插件 (P0)
- M-AGT-07：主动陪伴插件 (P1)
- M-AGT-08：高级情绪识别插件 (P2)

#### AI能力支撑层
- M-AI-01：ASR语音识别 (P0)
- M-AI-02：TTS语音合成 (P0)
- M-AI-03：基础意图理解 (P0)
- M-AI-04：轻量行为识别接口 (P1)
- M-AI-05：多模态融合优化 (P2)

#### 数据持久层
- M-DATA-01：用户档案存储 (P0)
- M-DATA-02：提醒任务存储 (P0)
- M-DATA-03：告警日志存储 (P0)
- M-DATA-04：日志定期清理 (P1)
- M-DATA-05：社区批量存储优化 (P2)

## 开发规范

### 目录结构

```
ElderGuard/
├── elder-agent/              # 老人端 Python 项目
│   └── src/
│       ├── core/             # 核心循环、状态管理
│       ├── plugins/          # 业务插件
│       ├── ai/               # AI能力封装
│       └── utils/            # 工具函数
├── cloud-server/             # 云端 FastAPI
│   └── app/
│       ├── routers/          # API路由
│       ├── models/           # 数据模型
│       ├── services/         # 业务逻辑
│       ├── core/             # 核心配置
│       ├── plugins/          # Agent插件
│       └── ai/               # AI能力封装
├── family-miniapp/           # 微信小程序
│   ├── pages/                # 页面
│   ├── components/           # 组件
│   └── utils/                # 工具函数
├── docs/                     # 文档
├── .gitignore
├── AGENTS.md
└── README.md
```

### 代码规范

#### Python (老人端/云端)
- 遵循 PEP 8 规范
- 使用类型注解
- 函数和类必须有文档字符串
- 异步函数使用 `async/await`

#### 微信小程序
- 遵循微信小程序开发规范
- 页面文件结构：`.wxml` / `.wxss` / `.js` / `.json`
- 组件命名使用驼峰式

### 设计模式

| 模式 | 应用位置 | 作用 |
|------|---------|------|
| 插件模式 | Agent微内核业务插件 | 统一接口，支持动态扩展 |
| 观察者模式 | 事件总线 | 模块解耦 |
| 策略模式 | 分级告警、灵敏度配置 | 不同等级不同逻辑 |
| 代理模式 | Agent作为代理 | 屏蔽底层细节 |
| 门面模式 | AI能力支撑层 | 统一接口 |
| 单例模式 | 事件总线、调度器 | 全局统一分发 |

### 插件接口规范

所有业务插件必须实现以下接口：

```python
class BasePlugin:
    def init(self) -> None:
        """插件初始化"""
        pass
    
    def start(self) -> None:
        """启动插件"""
        pass
    
    def stop(self) -> None:
        """停止插件"""
        pass
    
    def handle_event(self, event: Event) -> None:
        """处理事件"""
        pass
```

### 事件格式

```json
{
  "eventId": "string",
  "type": "fall|sedentary|voice|config|alert",
  "priority": "P0|P1|P2",
  "timestamp": "ISO8601",
  "source": "edge|cloud|family",
  "payload": {},
  "authToken": "string"
}
```

### REST接口规范

- 基础路径：`/api/v1`
- 认证方式：JWT Token
- 错误码：400/401/403/404/500
- 响应格式：

```json
{
  "code": 200,
  "message": "success",
  "data": {}
}
```

## 优先级说明

| 优先级 | 含义 | 开发策略 |
|--------|------|----------|
| P0 | 原型必须实现，答辩可演示 | 必须完整实现 |
| P1 | 尽量实现，允许部分模拟 | 可使用模拟逻辑 |
| P2 | 仅预留抽象接口，不完整编码 | 只定义接口 |

## 关键约束

### 硬约束（必须遵守）
- 系统定位为辅助看护，不替代医疗诊断
- 感知采集必须经隐私授权
- 未授权采集次数为0
- P0不依赖视觉，以主动语音求助为安全兜底

### 技术约束
- 老人端：Python + Vosk
- 家属端：微信小程序
- 云端：FastAPI
- 数据库：SQLite（原型）
- AI能力：外部API（阿里云 + 通义千问）

## 外部依赖

### 需要配置的API Key
1. 阿里云 AccessKey ID/Secret
2. 阿里云语音识别 App Key
3. 阿里云语音合成 App Key
4. 阿里云短信服务 AccessKey
5. 通义千问 API Key

### 配置文件位置
- 云端：`cloud-server/.env`
- 老人端：`elder-agent/.env`

## 运行命令

### 云端服务
```bash
cd cloud-server
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 老人端
```bash
cd elder-agent
pip install -r requirements.txt
python src/main.py
```

### 微信小程序
使用微信开发者工具打开 `family-miniapp` 目录

## 测试规范

### 单元测试
- 云端：使用 pytest
- 老人端：使用 pytest
- 小程序：使用 miniprogram-automator

### 集成测试
- P0功能必须全部通过
- 关键质量场景必须验证
- 硬约束必须满足

## 文档要求

- 代码必须有注释和文档字符串
- API接口必须有OpenAPI文档
- 重大变更必须更新README
