# DictaShift（语键）

Windows 常驻语音输入工具。按住 **F8** 中文听写、按住 **F9** 中文转英文输入，松开即把识别/翻译结果输入到当前光标位置。

音频与语音识别全部在本地完成（基于 [FunASR](https://github.com/modelscope/FunASR) 的 SenseVoice ONNX int8 模型），不依赖云端 ASR；翻译可选用免登录的 Google/Bing，或自带的 OpenAI 兼容接口（含本地 llama-server）。

## 功能

- 状态机驱动的录音/识别/翻译流水线：空闲 → 录音 → 识别中 → 输出。
- 系统托盘常驻：启用开关、打开设置、退出。
- 录音状态悬浮窗（可选常驻，配合结果展示）。
- 两种结果展示模式：
  - `popup`：直输失败时右下角弹出结果卡片（带复制按钮）；
  - `overlay`：结果直接显示在常驻悬浮窗里。
- 翻译：
  - 内置免登录的 Google 翻译 / Bing 翻译；
  - OpenAI 兼容接口（自填 `base_url` / `model` / `api_key`）；
  - 内置本地 llama-server 预设（默认端口 19120）。
- 模型本地下载：首次运行在「设置 - 语音识别」页点击下载，从 GitHub Releases 拉取并解压到 `%APPDATA%\DictaShift\models`。

## 快捷键

默认绑定（可在「设置 - 快捷键」中修改）：

| 动作 | 默认快捷键 | 说明 |
| --- | --- | --- |
| 中文听写 | `F8` | 按住开始录音，松开识别并输入中文到光标处 |
| 中文转英文 | `F9` | 按住开始录音，松开识别中文并翻译成英文输入 |
| 启用/暂停 | `Ctrl+Alt+F12` | 切换全局启用状态 |
| 取消 | `Esc` | 录音/识别中取消当前任务 |

## 安装

### 方式一：发布包（推荐普通用户）

1. 到 [Releases](https://github.com/yuchen-ya/dictashift/releases) 下载最新的 `DictaShift-<version>-windows.zip`。
2. 解压到任意目录，双击运行 `DictaShift.exe`。
3. 首次运行后，打开「设置 - 语音识别」，点击「下载模型」拉取 SenseVoice int8 模型（约 230 MB）。
4. 下载完成即可使用 F8 / F9。

### 方式二：源码运行

需要 Python 3.11 与 [uv](https://docs.astral.sh/uv/)。

```bash
# 1. 安装依赖
uv sync

# 2. 放置模型（二选一）
#    a) 应用内下载：启动后在「设置 - 语音识别」点击「下载模型」；
#    b) 手动放置：把 SenseVoice int8 模型三个文件放到 assets/models/sensevoice_onnx/
#       SenseVoice-Encoder.int8.onnx
#       SenseVoice-CTC.int8.onnx
#       Tokenizer.bpe.model

# 3. 运行
uv run python -m dictashift
```

## 翻译配置

在「设置 - 翻译」页选择「当前翻译配置」：

- **Google 翻译（免费）** / **Bing 翻译（免费）**：内置免登录，直接可用（翻译文本会发送到对应服务）。
- **OpenAI 兼容**：填写 `base_url`、`model`、`api_key`、`temperature`，可对接任何兼容 OpenAI Chat Completions 协议的服务。
- **本地 llama-server**：[llama.cpp](https://github.com/ggerganov/llama.cpp) 的 `llama-server` 即为 OpenAI 兼容服务，默认端口 19120。先自行启动 `llama-server`，再在本应用里填 `base_url=http://127.0.0.1:19120/v1`。

「测试连接」会用一句「你好」实际调用所选翻译服务，反馈成功/失败。

## 系统要求

- Windows 10 / 11 x64。
- Visual C++ 运行库（VC++ Redistributable）。
- 麦克风。

## 隐私说明

- 音频**仅在本地处理**：录音、SenseVoice ONNX 推理全部在本地完成，不上传音频。
- 翻译文本会发送到你所选择的翻译服务（Google / Bing / 你配置的 OpenAI 兼容服务）。如需完全离线，请使用本地 llama-server。
- 配置文件与模型存放在 `%APPDATA%\DictaShift\`，不收集任何遥测。
- **API Key 存储**：OpenAI 兼容配置的 API Key 以明文保存在 `%APPDATA%\DictaShift\settings.json`（与本类桌面客户端的普遍做法一致）。请确保系统账户安全，不要在共享机器上存放高权限 Key。
- **日志**：诊断日志写入 `%APPDATA%\DictaShift\logs\dictashift.log`，不包含 API Key、Authorization 头与翻译响应内容；出错时仅记录错误类型与状态码。
- **全局键盘钩子**：本应用通过全局快捷键（F8/F9 等）工作，需要监听键盘按下/松开事件（pynput）。应用只在快捷键边沿触发动作，不记录、不存储、不外传任何按键序列；部分杀毒软件可能对键盘钩子行为告警，属正常现象。
- **模型下载完整性**：语音识别模型从本仓库 GitHub Releases 下载，应用会校验文件大小与 SHA256。

## 开发

```bash
uv sync
uv run pytest          # 运行测试（硬件相关测试默认跳过）
uv run python -m dictashift
```

目录结构：

```
src/dictashift/
  main.py              程序入口（QApplication + logging）
  application.py       应用装配层（信号桥接、生命周期）
  core/                状态机、controller、jobs
  asr/                 ASR 服务、SenseVoice ONNX 引擎、模型下载
  audio/               录音、设备、重采样
  hotkeys/             全局快捷键监听（pynput）
  output/              光标处输入、剪贴板
  translation/         翻译后端（google/bing/openai 兼容/llama-server）
  ui/                  托盘、悬浮窗、设置窗口、结果弹窗
  settings/            配置模型与持久化仓库
assets/models/         本地模型存放点（开发兜底，发布包不含）
scripts/               打包脚本（build.py、DictaShift.spec）
tests/                 单元测试
```

## 打包

```bash
uv run python scripts/build.py
```

脚本调用 PyInstaller（`scripts/DictaShift.spec`，one-dir、windowed），产物输出到 `dist/DictaShift/`。

发布维护说明（打包 zip 之外，模型单独走 Releases）：

1. 把 SenseVoice int8 三个模型文件打成 `sensevoice_onnx-int8.zip`。
2. 上传到对应版本 tag 的 [Releases](https://github.com/yuchen-ya/dictashift/releases) 资产（当前代码固定下载地址指向 `v0.2.0` tag 的 `sensevoice_onnx-int8.zip`）。
3. 应用内「下载模型」按钮会从该地址拉取并解压校验。

## Roadmap

- macOS（Apple Silicon）支持。
- 内建 llama-server 进程管理（自动拉起/停止本地翻译服务）。
- DirectML / GPU 推理后端。

## License

MIT License，Copyright (c) 2026 yuchen-ya。详见 [LICENSE](LICENSE)。

语音识别模型来自 FunASR / SenseVoice 项目，其许可证以官方仓库为准。
