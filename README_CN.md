# whisplay-talk

[English](README.md)

基于 Whisplay HAT 的 P2P 语音对讲应用，面向多台 Whisplay 设备之间的局域化语音广播场景。

这个版本已经把核心链路搭起来了：
- 以 `whisplay-daemon` app 的形式接入和启动
- 基于 Tailscale `MagicDNS` 发现在线设备，约定设备名以 `whisplay-talk-` 开头
- 按住按钮讲话时，将麦克风音频压缩后通过 UDP 单播到所有在线 peer
- 其他设备实时播放，并在屏幕显示当前说话设备
- 空闲时屏幕显示当前在线对讲设备列表

## 当前实现

当前仓库是一个可运行 MVP，技术方案如下：

- 发现：
  通过 `tailscale status --json` 找到主机名以 `whisplay-talk-` 开头的设备，再额外探测每台设备的 app TCP 端口，只有探测成功才标记为在线
- 传输：
  所有设备监听固定 TCP 端口 `24680` 进行音频流传输
- 音频：
  使用 `arecord` / `aplay` 录放音，默认 16kHz / 16-bit / mono 采集，配合 `Opus` 语音编码、接收端轻量抖动缓冲，以及单帧冗余重发
- 显示：
  使用 Pillow 渲染 240x280 UI，并写入 `whisplay-daemon` 提供的 framebuffer
- 输入：
  通过 `whisplay-daemon` 的按钮事件实现按住说话

## 目录结构

```text
whisplay-talk/
├── main.py
├── application.py
├── config.py
├── audio/
├── display/
├── hardware/
├── network/
├── install.sh
├── run.sh
├── requirements.txt
└── .env.template
```

## 安装

```bash
git clone <this-repo>
cd whisplay-talk
bash install.sh
```

`install.sh` 会做这些事：
- 安装 Python / ALSA utils / curl / `libopus0`
- 创建 `venv`
- 安装 `Pillow` 和 `python-dotenv`
- 下载字体 `NotoSansSC-Bold.ttf`
- 在检测到 `whisplay-daemon` 时自动注册 app

## 配置

先复制配置文件：

```bash
cp .env.template .env
```

关键配置：

- `WHISPLAY_TALK_DEVICE_PREFIX`
  默认 `whisplay-talk-`
- `WHISPLAY_TALK_DEVICE_NAME`
  留空时自动取系统 hostname；如果 hostname 没有此前缀，会自动补上
- `WHISPLAY_TALK_UDP_PORT`
  默认 `24680`
- `ALSA_INPUT_DEVICE`
  录音设备，默认 `default`
- `ALSA_OUTPUT_DEVICE`
  播放设备，默认 `default`
- `AUDIO_CODEC`
  默认 `opus`，是当前这版实时对讲推荐配置
- `AUDIO_FRAME_MS`
  默认 `40`，降低发包频率，通常能改善弱链路下的连续性
- `AUDIO_REDUNDANCY_FRAMES`
  默认 `1`，会顺带重发上一帧压缩音频，用来补单帧丢包
- `AUDIO_OPUS_BITRATE`
  默认 `16000`，更偏向语音连续性
- `AUDIO_OPUS_COMPLEXITY`
  默认 `6`，在树莓派 CPU 开销和音质之间进一步做平衡
- `AUDIO_OPUS_PACKET_LOSS_PERC`
  默认 `15`，告诉 Opus 编码器按有丢包的链路来优化
- `AUDIO_OPUS_ENABLE_FEC`
  默认 `1`，开启 Opus 自带前向纠错
- `WHISPLAY_TALK_RECEIVE_PREBUFFER_FRAMES`
  默认 `24`，按当前 40ms Opus 帧约等于先缓存 1 秒再播放

建议所有设备都统一设置 hostname，例如：

```bash
sudo hostnamectl set-hostname whisplay-talk-kitchen
sudo hostnamectl set-hostname whisplay-talk-office
```

并确保这些设备都已经加入同一个 Tailscale tailnet。

## 运行

直接运行：

```bash
bash run.sh
```

如果系统里运行了 `whisplay-daemon`，建议从 daemon 的 app 列表进入 `Talk`。

如果设备不使用 `whisplay-daemon`，可以通过下面的脚本配置开机自启动：

```bash
bash startup.sh
```

`startup.sh` 会为当前应用安装一个 `systemd` 服务；如果检测到机器上已经有 `whisplay-daemon`，脚本会直接退出，不做额外配置。

## 交互说明

- 空闲时：
  屏幕显示在线设备列表
- 如果设备没有安装 Tailscale：
  屏幕显示安装提醒
- 如果设备安装了 Tailscale 但未登录或未运行：
  屏幕显示对应的登录/启动提示
- 按住按钮：
  本机进入 `Speaking`
- 松开按钮：
  停止发送，并发送一个结束包
- 远端收到音频：
  进入 `Receiving`，播放音频并显示谁在讲话

## UDP 包格式

当前使用一个轻量自定义头：

- magic: `WT01`
- type: `1`
- flags:
  `1 = start`, `2 = end`
- sender name
- stream id
- sequence
- codec id
- 压缩音频 payload，当前默认是 `Opus`
- 可选的上一帧冗余 payload

这让我们后续很容易继续演进到：
- Opus 压缩
- 单播优先级
- 对讲占线控制
- 半双工/全双工策略
- 更强的丢包恢复

## 已知边界

当前版本是 MVP，还没有做这些能力：

- 现在默认已经切到 `Opus`，但传输层仍然是轻量自定义协议，还不是完整媒体栈
- 没有占线仲裁，两个设备同时讲话时会以“最后到达的流”为主
- 目前优先走 `whisplay-daemon`，未内建完整 direct hardware fallback
- 没有在线成员昵称管理，默认从 hostname 派生

## 下一步建议

如果我们继续往下做，最值得接着补的是：

1. 引入 Opus，降低 Tailscale 上的带宽占用
2. 增加一个简单的占线锁，避免多人同时抢麦
3. 细化 UI，做成更接近 `whisplay-ai-chatbot` 的状态表现
4. 增加 daemon 安装产物，例如 desktop entry / app manifest

## License

本项目采用 GPL-3.0 许可证。详见 [LICENSE](LICENSE)。
