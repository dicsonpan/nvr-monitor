# NVR Monitor · 轻量监控录像与实时预览

一个跑在 ARM 单板机（如 RK3399）上的轻量级 NVR：

- **录像**：ffmpeg `-c copy` 零转码直接落盘，保留摄像头主码流原始画质（2K/HEVC 实测可行）
- **实时预览**：RTSP 子码流 → HLS 切片，浏览器多路九宫格观看
- **磁盘自愈**：写满时按时间滚动删除最旧的分片，永不写爆根分区
- **CPU 限流**：按需转码 + 并发上限 + 空闲自停 + 降帧 + 分组轮播，软编也能带 10 路
- **混合品牌**：逐台配置认证方式与子码流路径，不同厂家摄像头共存

纯 Python 标准库 + ffmpeg，**零 pip 依赖**。

---

## 功能特性

| 模块 | 说明 |
|---|---|
| 录像守护 `nvr-recorder.sh` | 每路独立进程 `-c copy` 录制，10 分钟一片；断线指数退避重连；磁盘超阈值自动清理 |
| Web 服务 `nvr-server.py` | 录像检索/回放 + 实时预览(HLS) + 系统资源面板；纯 `http.server`，默认 80 端口 |
| 前端 `index.html` | 九宫格实时预览、时间轴回放、下载、系统负载展示；hls.js 已本地化，不依赖公网 CDN |
| 日志看门狗 `log-guard.sh` | 根分区将满时自动截断最大的日志，防止小容量 eMMC 被日志写爆 |
| 服务单元 `nvr-recorder.service` | systemd 托管 |

---

## 系统架构

```
                         ┌─────────────── 摄像头(多品牌, RTSP) ───────────────┐
                         │  主码流 /stream1 (2K, 录制用)                       │
                         │  子码流 / 或 /stream2 (800x448, 预览用)             │
                         └───────────────┬───────────────────────────────────┘
                                         │ RTSP
                 ┌───────────────────────┴────────────────────────┐
          录像(零转码)                                     实时预览(软编→HLS)
   nvr-recorder.sh  -c copy                          nvr-server.py
   每路 ffmpeg → /recordings/*.mp4                   按需启动 ffmpeg → /hls/<cam>/*.ts
                                                         (MAX_STREAMS 并发上限, 空闲自停)
                 └───────────────────────┬────────────────────────┘
                                         ▼
                              浏览器 http://<板子IP>  (index.html 九宫格)
```

**为什么录像零转码、预览才转码？**
- 录像要保留证据级画质，且 10 路同时软编单板机扛不住 → 直接 `copy` 最省
- 预览只是给人看，子码流本身分辨率低，转成 HLS 兼容的 H.264 即可

**为什么用 HLS 做预览？**
浏览器不能直接播 RTSP。HLS 纯 HTTP 穿透性最好、抗抖动（切片后网络波动不会让整段卡死），延迟 1–2 秒对安防足够。Safari/iOS 原生支持，桌面 Chrome 用本地 `static/hls.min.js`。

---

## 硬件约束与对应设计（通用经验）

这套系统是在一台 **6 核 ARM（RK3399 同级）、根分区仅 15G eMMC、数据盘挂在大容量 USB 盘** 的盒子上打磨出来的。两个硬约束塑造了上面的设计：

1. **根分区小，日志会把它写爆。**
   曾因某进程对离线摄像头每 3 秒刷重连日志，几小时写出数 GB 把系统卡死。
   对策：日志看门狗（剩余不足阈值即截断大日志）+ 录制脚本断线**指数退避**重连（绝不固定 3 秒死循环）+ logrotate 调成 daily+20M。

2. **板子硬编不可用，只能软编。**
   RK3399 的 VPU 对当前 ffmpeg 支持不全，实测直接挂死。软编单路 800×448 约 80–110% CPU，
   所以**并发上限锁在 4 路**，并叠加降帧（12fps）和分组轮播，把总占用压到 300% 出头。

> 若你的板子有可用硬编（如带 `/dev/video*` 且 ffmpeg 支持 `h264_v4l2m2m`），可把预览命令换成硬编，并发能大幅提高。

---

## 目录结构

```
nvr-monitor/
├── nvr-recorder.sh        # 录像守护(零转码 + 磁盘清理)
├── nvr-server.py          # Web 服务(回放 + 实时预览 + 资源面板)
├── index.html             # 前端页面(放服务端同目录, 由 / 直接返回)
├── static/
│   └── hls.min.js         # 本地化 hls.js(Chrome/Firefox 播放 HLS 需要)
├── log-guard.sh           # 日志看门狗(cron 调用)
├── nvr-recorder.service   # systemd 单元示例
├── .gitignore
├── LICENSE
└── README.md
```

> 部署时 `nvr-server.py` 与 `index.html` 需放在同一目录；`static/` 用于 hls.js 等前端资源。

---

## 依赖

- `ffmpeg`（需带 `libx264` 与 `hls` muxer；arm64 发行版自带或 `apt install ffmpeg`）
- `python3`（3.7+，仅用标准库，无需 `pip install`）
- 摄像头开启 RTSP（主码流 + 子码流）

---

## 部署

```bash
# 1. 放到板子
mkdir -p /opt/nvr/bin
cp nvr-recorder.sh nvr-server.py index.html log-guard.sh /opt/nvr/bin/
cp -r static /opt/nvr/bin/static
chmod +x /opt/nvr/bin/nvr-recorder.sh /opt/nvr/bin/log-guard.sh

# 2. 数据盘(大容量)挂载点, 默认 /mnt/nvr
#    recordings / hls / logs / state 会自动创建
mkdir -p /mnt/nvr && mount <你的数据盘> /mnt/nvr

# 3. 开机自启
cp nvr-recorder.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now nvr-recorder
# Web 服务也可放进同一个或单独的 systemd 单元, 例如:
#   /opt/nvr/bin/nvr-server.py  (默认监听 0.0.0.0:80)

# 4. 日志看门狗(每 10 分钟)
echo '*/10 * * * * /opt/nvr/bin/log-guard.sh' | crontab -
```

浏览器访问 `http://<板子IP>/` 即可看到九宫格监控中心。

---

## 配置

### 1. 摄像头清单（两处都要改，保持一致）

**`nvr-recorder.sh` 顶部 `CAMERAS` 数组**（格式 `名称|IP`）：

```bash
CAMERAS=(
    "Camera1|192.168.1.21"
    "Camera2|192.168.1.22"
    # ...
)
```

**`nvr-server.py` 顶部 `CAMERAS` 列表**：

```python
CAMERAS = [
    {"name": "Camera1", "ip": "192.168.1.21", "label": "Camera 1", "zone": "入口"},
    # 需要认证的厂家, 加 user/pass/sub 字段:
    {"name": "Camera5", "ip": "192.168.1.25", "label": "Camera 5", "zone": "教室 C",
     "user": "admin", "pass": "YOUR_CAM_PASSWORD", "sub": "/stream2"},
]
```

### 2. 混合品牌的认证 / 子码流差异

不同厂家差异很大，系统按**每台**配置，不用一套全局参数：

| 项 | 厂家 A | 厂家 B |
|---|---|---|
| 主码流 | `rtsp://<IP>:554/stream1` | `rtsp://admin:密码@<IP>:554/stream1` |
| 子码流 | 根路径 `/`（800×448） | `/stream2`（800×448） |

在 `nvr-recorder.sh` 里改这三个函数（示例按 IP 段区分）：

```bash
cam_auth() {   # 返回 "用户名 密码"(空格分隔, 无认证返回空)
    case "$1" in
        192.168.1.2[5-8]) echo "admin YOUR_CAM_PASSWORD" ;;
        *) echo "" ;;
    esac
}
cam_main() { echo "/stream1"; }          # 主码流路径(多数厂家一致)
cam_sub()  {                              # 子码流路径(厂家差异大)
    case "$1" in
        192.168.1.2[5-8]) echo "/stream2" ;;
        *) echo "/" ;;
    esac
}
```

> ⚠️ `cam_auth` 用**空格**分隔用户名密码，再在 `build_url` 里拆开。
> 千万别用 `|` 拼进 URL —— 竖线会被当成密码一部分，导致 RTSP 401。

`nvr-server.py` 里对应有 `sub_url(cam)`，读摄像头字典里的 `user`/`pass`/`sub` 字段。

### 3. 预览转码参数（省 CPU 的关键）

```python
MAX_STREAMS   = 4        # 并发转码上限, 软编别超过 4
PREVIEW_FPS   = 12       # 降帧最省 CPU(约省 28%), 监控够用
PREVIEW_PRESET= "veryfast"
PREVIEW_CRF   = "26"     # 监控场景 crf23→26 肉眼几乎无差别
PREVIEW_W, PREVIEW_H = 800, 448
```

### 4. 磁盘清理阈值

```bash
KEEP_PCT=85          # 使用率达 85% 开始清理(给写入留缓冲)
KEEP_PCT_HARD=95     # 95% 立即清理
MIN_FREE_GB=15       # 无论如何保留 15G
```

---

## 运维命令

```bash
/opt/nvr/bin/nvr-recorder.sh status     # 各路录制状态 + 今日产出 + 磁盘
/opt/nvr/bin/nvr-recorder.sh stop       # 停止全部录制
/opt/nvr/bin/nvr-recorder.sh start      # 启动
/opt/nvr/bin/nvr-recorder.sh cleanup    # 立即触发一次清理

# Web API(浏览器/脚本调用)
curl http://<板子IP>/api/ping           # 各摄像头在线状态
curl http://<板子IP>/api/overview       # 录像清单(按日期)
curl http://<板子IP>/api/sysinfo        # CPU/内存/温度/负载
curl http://<板子IP>/api/live           # 实时预览转码状态
```

---

## 已知边界

- 单板机软编上限约 4 路并发预览；要更多并发需硬编或更强 CPU。
- 录像为 `-c copy`，依赖摄像头主码流本身可被 ffmpeg 直接封装（H.264/H.265 + MP4 通常没问题）。
- 示例里的 IP（`192.168.1.x`）与摄像头密码（`YOUR_CAM_PASSWORD`）均为占位，部署时替换为你自己的。

---

## License

[MIT](LICENSE) — 随意使用、修改、商用，保留版权声明即可。
