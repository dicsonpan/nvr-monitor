# NVR Monitor · 轻量监控录像与实时预览

一个跑在 ARM 单板机（如 RK3399）上的轻量级 NVR：

- **录像**：ffmpeg `-c copy` 零转码直接落盘，保留摄像头主码流原始画质（2K/HEVC 实测可行）
- **实时预览**：RTSP 子码流 → **自适应三段式管道**（零转码直通 / VPU 硬编 / 软编）→ HLS 切片，浏览器多路九宫格观看
- **磁盘自愈**：写满时按时间滚动删除最旧的分片，永不写爆根分区
- **按模式限流**：H.264 子码流直通不占 CPU 额度，HEVC 才转码；10 路可同时实时
- **混合品牌**：逐台配置认证方式与子码流路径，不同厂家摄像头共存

纯 Python 标准库 + ffmpeg，**零 pip 依赖**。

---

## 功能特性

| 模块 | 说明 |
|---|---|
| 录像守护 `nvr-recorder.sh` | 每路独立进程 `-c copy` 录制，10 分钟一片；断线指数退避重连；磁盘超阈值自动清理 |
| Web 服务 `nvr-server.py` | 录像检索/回放 + 实时预览(自适应管道) + 系统资源面板；纯 `http.server`，默认 80 端口 |
| 前端 `index.html` | 九宫格实时预览、时间轴回放、下载、系统负载展示；hls.js 已本地化，不依赖公网 CDN |
| 诊断脚本 `probe-cameras.sh` | 板端一键探测每路子码流编码/分辨率/帧率 + 板子硬编能力，给出建议模式 |
| 部署脚本 `deploy-to-goldlion.sh` | 灌入真实配置 → 板端备份 → 上传 → 语法检查 → 重启 → 验证，一条龙 |
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
          录像(零转码)                                     实时预览(自适应管道)
   nvr-recorder.sh  -c copy                          nvr-server.py
   每路 ffmpeg → /recordings/*.mp4                   ① 逐路 ffprobe 探测子码流编码(缓存6h)
                                                         │
                          ┌──────────────────────────────┼──────────────────────────┐
                          ▼                              ▼                          ▼
                    H.264 子码流                    HEVC 子码流                硬编不可用
                  ② 零转码直通                     ③ VPU 硬编(自动探测)         ④ 软编 libx264
                  ffmpeg -c:v copy                 hevc_v4l2m2m 解               (并发上限4路)
                  单路 CPU 约 2-5%                 + h264_v4l2m2m 编
                  不占转码额度                      (并发上限8路)
                          └──────────────────────────────┬──────────────────────────┘
                                                         ▼
                              浏览器 http://<板子IP>  (index.html 九宫格, 10 路全实时)
```

**为什么录像零转码、预览却要分三种模式？**
- 录像要保留证据级画质，且 10 路同时软编单板机扛不住 → 直接 `copy` 最省
- 预览的关键洞察：**不是每路都需要转码**。先探测子码流编码——
  - 本来就是 H.264（多数摄像头默认）→ `-c:v copy` 直接 remux 成 HLS，CPU 约等于零，10 路全开无压力
  - 是 HEVC（浏览器普遍不解）→ 优先 VPU 硬编；板子硬编不可用（老 ffmpeg 驱动不了 multiplanar 编码节点）→ 自动回退软编
- 摄像头后台把子码流改成 H.264 是最优解：相机端一个设置，板子零成本

**为什么用 HLS 做预览？**
浏览器不能直接播 RTSP。HLS 纯 HTTP 穿透性最好、抗抖动（切片后网络波动不会让整段卡死），延迟 1–2 秒对安防足够。Safari/iOS 原生支持，桌面 Chrome 用本地 `static/hls.min.js`。

---

## 硬件约束与对应设计（通用经验）

这套系统是在一台 **6 核 ARM（RK3399 同级）、根分区仅 15G eMMC、数据盘挂在大容量 USB 盘** 的盒子上打磨出来的。三个硬约束塑造了上面的设计：

1. **根分区小，日志会把它写爆。**
   曾因某进程对离线摄像头每 3 秒刷重连日志，几小时写出数 GB 把系统卡死。
   对策：日志看门狗（剩余不足阈值即截断大日志）+ 录制脚本断线**指数退避**重连（绝不固定 3 秒死循环）+ logrotate 调成 daily+20M。

2. **板子硬编可用性是「探测」出来的，不是假设的。**
   RK3399 有 VPU 编码节点（`/dev/video2`，mainline 内核驱动 `rockchip,rk3399-vpu-enc`），
   但 ffmpeg 4.2.7（Ubuntu focal）的 `h264_v4l2m2m` 对 multiplanar 节点支持不全，
   实测直接报 `Could not find a valid device`。**服务启动时会实测一条**
   （lavfi 测试源 → 硬编 → null），不可用就自动走软编，不需要人工判断。
   若把 ffmpeg 升级到 5.1+（静态包即可），硬编大概率可直接启用，HEVC 路也能省 CPU。

3. **软编单路约 81% CPU（RK3399 实测 800×448 输入）。**
   所以软编并发锁 4 路（约 324%）；直通路不占额度（单路 2–5%）。
   实测金利来现网：**10 路全开（6 直通 + 4 软编）CPU 约 60%、温度约 54°C**，
   而旧设计 5 路全软编就要 71%、64°C。

> 若摄像头后台可把子码流设为 H.264（多数可以），优先改相机端——
> 那是零成本方案；板子这边什么都不用动，重启或 `curl /api/reprobe` 后自动转直通。

---

## 目录结构

```
nvr-monitor/
├── nvr-recorder.sh        # 录像守护(零转码 + 磁盘清理)
├── nvr-server.py          # Web 服务(回放 + 自适应实时预览 + 资源面板)
├── index.html             # 前端页面(放服务端同目录, 由 / 直接返回)
├── static/
│   └── hls.min.js         # 本地化 hls.js(Chrome/Firefox 播放 HLS 需要)
├── probe-cameras.sh       # 板端诊断: 逐路子码流编码/帧率 + 硬编能力 + 建议模式
├── deploy-to-goldlion.sh  # 一键部署到金利来板子(灌配置+备份+重启+验证)
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

### 3. 预览模式参数（自适应管道的三个额度）

```python
MAX_SW_STREAMS = 4      # 软编并发上限(RK3399 实测 4 路约 324% CPU, 不能再高)
MAX_HW_STREAMS = 8      # 硬编并发上限(VPU 编码能力余量大; 本板 ffmpeg4.2.7 实测不可用)
MAX_TOTAL      = 12     # 总并发兜底(含直通)
SLICE_SECS     = {"copy": 2, "hw": 3, "sw": 4}   # 直路切片更短, 延迟更低
PROBE_TTL      = 6*3600 # 子码流探测结果缓存; 相机端改编码后调 /api/reprobe 立即失效

# 软编参数(仅 HEVC 子码流且硬编不可用时使用, 单路约 81% CPU):
PREVIEW_FPS   = 12       # 降帧最省 CPU(约省 28%), 监控够用
PREVIEW_PRESET= "veryfast"
PREVIEW_CRF   = "26"     # 监控场景 crf23→26 肉眼几乎无差别
```

模式是**每路独立**的：H.264 子码流永远走直通（不占软编额度），
只有 HEVC 子码流占用软编/硬编额度。前端按 `/api/live` 返回的各路模式
显示徽章（直通=绿 / 硬编=蓝 / 软编=橙），软编满时显示「等待中」并自动排队。

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

/opt/nvr/bin/probe-cameras.sh           # 诊断: 逐路子码流编码/帧率 + 板子硬编能力 + 建议模式

# Web API(浏览器/脚本调用)
curl http://<板子IP>/api/ping           # 各摄像头在线状态
curl http://<板子IP>/api/overview       # 录像清单(按日期)
curl http://<板子IP>/api/sysinfo        # CPU/内存/温度/负载
curl http://<板子IP>/api/live           # 各路预览模式/在放状态/额度
curl http://<板子IP>/api/reprobe        # 相机端改过编码后: 重新探测全部子码流
```

> 相机后台把子码流从 HEVC 改成 H.264 后，跑一次 `/api/reprobe`
> （或 `systemctl restart nvr-web`），该路即从软编切为零转码直通。

---

## 已知边界

- 软编上限 4 路（RK3399 实测）；H.264 子码流直通不占额度，10 路全开实测 CPU 约 60%。
- 本板 ffmpeg 4.2.7 驱动不了 RK3399 的 multiplanar 编码节点，硬编自动探测不可用；
  升级 ffmpeg ≥ 5.1（静态包即可）后大概率可直接启用，无需改代码。
- 录像为 `-c copy`，依赖摄像头主码流本身可被 ffmpeg 直接封装（H.264/H.265 + MP4 通常没问题）。
- 示例里的 IP（`192.168.1.x`）与摄像头密码（`YOUR_CAM_PASSWORD`）均为占位，部署时替换为你自己的。

---

## License

[MIT](LICENSE) — 随意使用、修改、商用，保留版权声明即可。
