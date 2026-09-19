# whisplay-talk

<img src="https://docs.pisugar.com/img/whisplay_logo@4x-8.png" alt="Whisplay Talk" width="200" />

[English](README.md)

基于 Whisplay HAT 的 P2P 语音对讲应用，面向多台 Whisplay 设备之间的局域化语音广播场景。

核心功能：
- 以 `whisplay-daemon` app 的形式接入和启动
- 同时通过 Tailscale `MagicDNS` 和 ESP-NOW 心跳发现在线设备
- 按住按钮讲话时，将麦克风音频压缩后通过所有可用的 TCP / ESP-NOW 路径发给在线 peer
- 其他设备实时播放，高亮当前说话设备，并在状态框显示接收图标
- 空闲时屏幕显示设备列表、在线状态、心跳延时，以及明确的 `[ESP]` 或 `[TCP]` 传输标签

## 截图

<p align="center">
  <img src="assets/readme/idle.png" alt="Idle screen" width="32%" />
  <img src="assets/readme/speaking.png" alt="Speaking screen" width="32%" />
  <img src="assets/readme/receiving.png" alt="Receiving screen" width="32%" />
</p>

## 界面说明

- Header：
  显示 `WhisplayTalk` 标题，以及 VPN、Wi-Fi 信号、电池状态图标
- 状态框：
  显示当前 app 状态、本机设备名、实时 `ESP CH n` 射频信道，以及接收音频时右侧的说话图标
- 设备列表：
  即使在讲话或接收时也持续显示 peer 列表，包含在线/离线标记、传输标签和心跳延时，例如 `kitchen [TCP] (42ms)`
- 当前讲话高亮：
  当前正在讲话的设备会以黄色高亮
- 底部提示：
  显示当前动作提示，例如 `Hold button to talk`、`Release to stop` 或 `Listening...`

## 当前实现

当前采用的技术方案如下：

- 发现：
  通过 `tailscale status --json` 找到主机名以 `whisplay-talk-` 开头的设备，再额外探测每台设备的 app TCP 端口，只有探测成功才标记为在线，并记录心跳延时
- 传输：
  所有设备监听固定 TCP 端口 `24680` 进行音频流传输
- 音频：
  使用 `arecord` / `aplay` 录放音，默认 16kHz / 16-bit / mono 采集，配合 `Opus` 语音编码、接收端轻量抖动缓冲，以及单帧冗余重发
- 显示：
  使用 Pillow 渲染 240x280 UI，并写入 `whisplay-daemon` 提供的 framebuffer，包含 header 的 VPN / Wi-Fi / 电池图标和动态设备列表
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

### Raspberry Pi Zero 2 W Nexmon 固件

已经过实机验证的 BCM43430/1 Nexmon 固件、CM5 预编译内核模块、DKMS 源码包、`nexutil`、
开机服务、校验值和安装说明已保存在
[`firmware/nexmon-zero2w`](firmware/nexmon-zero2w/README.md)。该配置保留
`wlan0` 的普通 Wi-Fi 联网能力，同时提供同信道的 `mon0` 接口，用于
Radiotap/802.11 抓包和注入。

### ESP-NOW 传输

在已安装仓库内 Nexmon 固件的 Zero 2 W 上安装特权 radio bridge：

```bash
sudo bash tools/install_espnow_bridge.sh
```

使用自动生成的设备名或设置 `WHISPLAY_TALK_DEVICE_NAME`，然后从
`whisplay-daemon` 启动 Talk。Talk 本身仍以普通用户运行，通过
`/run/whisplay-espnow/bridge.sock` 与 root bridge 交换 Unix 数据报。设备发现使用
`WD01` 广播心跳，音频沿用现有 `WT01` 格式。ESP-NOW v1 应用 payload 上限为
250 字节，超限时会自动去掉冗余音频。当前传输采用广播，未加密、未认证。
`wlan0` 和 `mon0` 可以共存，但必须使用相同 Wi-Fi 信道。
TCP/Tailscale 与 ESP-NOW 会始终同时启动，不再需要选择传输方式。如果固定的本地
bridge socket 不存在，Talk 会自动降级为只使用 TCP。设备列表会根据 peer 的实际
可用路径显示 `[ESP]`、`[TCP]` 或 `[ESP/TCP]`。
普通 Wi-Fi 已关联时，ESP-NOW 始终跟随 AP 信道，bridge 不会主动换台。Wi-Fi
未关联时，所有新版本节点统一回落到信道 6；如果连续 12 秒没有发现 peer，bridge
会以随机、低占空比方式扫描信道 1/6/11，并在每次换台后立即发送发现帧。发现对端
后，两台新版本节点会一起回到信道 6。这样设备在没有 AP 的环境中启动也能自动
汇合，同时不会干扰已经建立的普通 Wi-Fi 连接。
离线期间 bridge 会启用 Nexmon 扫描抑制，防止 NetworkManager 后台扫频悄悄改变
ESP-NOW 信道；音频空闲时每分钟短暂恢复一次 Wi-Fi 扫描，使已配置的 AP 仍可重连。
为提高无线可靠性，bridge 会关闭 Wi-Fi 省电，使用带长前导码的 1 Mbps DSSS，
每个音频帧发送四次、每个发现心跳发送七次，并以带抖动的间隔分散副本，避免一次
短暂干扰同时破坏所有副本。bridge 还会定期重建 Nexmon 的 pcap 注入句柄，并每
十秒记录一次平滑后的 peer RSSI，供走距测试使用。Zero 2 W 校准数据将 2.4GHz
功率限制在约 19.5 dBm，因此不会强行越过校准功率以免增加失真。默认 Opus 码率
为 12 kbps，以缩短空口帧。

可在两台设备上同时运行应用层双机测试：

```bash
venv/bin/python tools/espnow_app_smoke.py
```

### AtomS3R 语音客户端

[`firmware/atom-s3r-whisplay-talk`](firmware/atom-s3r-whisplay-talk/README.md)
包含用于 M5Stack AtomS3R + Atomic Voice Base 的独立客户端。它与 Zero 2 W
bridge 使用相同的 `WD01` 发现协议和 `WT01`/Opus 音频协议。屏幕会显示状态与
已发现设备；按住屏幕讲话，松开结束。默认设备名为 `atomic-s3`，ESP-NOW
信道为 2。

`tools/espnow_audio_probe.py` 可从树莓派发送一段短 Opus 测试音，用于可重复地
验证 Atom 客户端的接收和播放。

## Tailscale 安装

所有设备都必须先加入同一个 Tailscale tailnet，`whisplay-talk` 才能发现彼此。

在树莓派上安装 Tailscale：

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

执行 `sudo tailscale up` 后，终端会输出一个登录链接。用浏览器打开该链接并完成设备登录。

可以通过下面的命令确认是否已经连上：

```bash
tailscale status
```

如果设备没有安装 Tailscale、还没登录，或者服务没有运行，app 会在屏幕上显示对应提示。

## 配置

先复制配置文件：

```bash
cp .env.template .env
```

关键配置：

- `WHISPLAY_TALK_DEVICE_PREFIX`
  默认 `whisplay-talk-`
- `WHISPLAY_TALK_DEVICE_NAME`
  可选的显式显示名称。留空时，app 首次启动会生成类似
  `amber-otter` 的两段式易读随机名，后续启动继续使用同一名称
- `WHISPLAY_TALK_DEVICE_NAME_FILE`
  默认 `~/.config/whisplay-talk/device-name`，用于持久化自动生成的名称；
  `WHISPLAY_TALK_DEVICE_NAME` 有值时始终优先
- `WHISPLAY_TALK_TCP_PORT`
  默认 `24680`
- `WHISPLAY_TALK_APP_HEARTBEAT_TIMEOUT_MS`
  默认 `3000`，用于 peer 在线探测和延时测量的超时
- `WHISPLAY_TALK_APP_HEARTBEAT_FAILS_BEFORE_OFFLINE`
  默认 `5`，允许连续多少次心跳探测失败后才把 peer 标记为离线
- `ALSA_INPUT_DEVICE`
  录音设备。留空时会优先自动识别 `whisplaysound`，并兼容旧 Whisplay 卡名，找不到再回退到 `default`
- `ALSA_OUTPUT_DEVICE`
  播放设备。留空时会优先自动识别 `whisplaysound`，并兼容旧 Whisplay 卡名，找不到再回退到 `default`
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
  普通网络默认 `24`，保留原先约 960ms 的抖动缓冲
- `WHISPLAY_TALK_PLAYOUT_PREFILL_FRAMES`
  普通网络默认 `6`，保留原先 240ms 的 ALSA 静音预填充
- `WHISPLAY_TALK_PLAYOUT_MISSING_GRACE_MS`
  普通网络默认 `200`，保留原先等待迟到包的方式
- `WHISPLAY_TALK_ESPNOW_RECEIVE_PREBUFFER_FRAMES`
  默认 `4`，按当前 40ms Opus 帧约等于缓存 160ms
- `WHISPLAY_TALK_ESPNOW_PLAYOUT_PREFILL_FRAMES`
  默认 `1`，ESP-NOW 仅使用一帧 40ms 静音预热播放设备
- `WHISPLAY_TALK_ESPNOW_PLAYOUT_MISSING_GRACE_MS`
  默认 `0`；未按时到达的 ESP-NOW 帧在原时间点用 Opus 隐藏，
  不暂停后续整段音频
- `AUDIO_PLAYER_BACKEND`
  默认 `aplay`，使用 200ms 设备缓冲抵抗无线抖动；`alsa` 保留用于诊断

## 设备命名

设备发现依赖 Tailscale `MagicDNS` 主机名。只有主机名以 `whisplay-talk-` 开头的设备，才会被识别为对讲 peer。

推荐命名方式：

- `whisplay-talk-kitchen`
- `whisplay-talk-room1`
- `whisplay-talk-office`

UI 显示时会自动去掉 `whisplay-talk-` 前缀，所以 `whisplay-talk-kitchen` 会显示成 `kitchen`。

更推荐直接在 Tailscale 管理后台修改设备名，把每台设备改成 `whisplay-talk-<name>` 这种形式。

例如：

- `whisplay-talk-kitchen`
- `whisplay-talk-room1`

在 Tailscale 后台改名后，等待新的 `MagicDNS` 名称同步到其他 peer 即可。

如果确实需要，也可以通过 `WHISPLAY_TALK_DEVICE_NAME` 单独覆盖本机 app 名称。
ESP-NOW 会把这个名字广播给 peer；TCP 模式下远端设备名称仍来自 Tailscale MagicDNS。

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
  屏幕显示设备列表，包含自己、在线/离线标记，以及 peer 心跳延时
- 如果设备没有安装 Tailscale：
  屏幕显示安装提醒
- 如果设备安装了 Tailscale 但未登录或未运行：
  屏幕显示对应的登录/启动提示
- 按住按钮：
  本机进入 `Speaking`，并先停掉本地播放，避免回音
- 松开按钮：
  停止发送，并发送一个结束包
- 远端收到音频：
  进入 `Receiving`，播放音频、显示谁在讲话，并在状态框右侧显示说话图标

## 音频流包格式

当前使用的是跑在 TCP 流上的轻量自定义包头：

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
- 单播优先级
- 对讲占线控制
- 半双工/全双工策略
- 更强的丢包恢复

## 已知边界

当前版本还是 MVP，当前比较明确的边界有这些：

- 传输层仍然是自定义 TCP 音频分帧，不是标准语音/媒体协议栈
- 还没有显式的占线锁或仲裁机制，多台设备同时抢麦时不会被协调管理
- 设备身份目前仍然直接从 Tailscale hostname 前缀派生，没有单独的昵称或联系人体系
- 最完整的体验仍然依赖 `whisplay-daemon`；`startup.sh` 只是帮助无 daemon 的系统开机启动 app，不等价于 daemon 那套 UI / runtime

## License

本项目采用 GPL-3.0 许可证。详见 [LICENSE](LICENSE)。
