#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NVR 监控服务
- 录像查询: 扫描 /mnt/nvr/recordings 下的 10 分钟分片, 时间轴检索 / 在线播放 / 下载
- 实时预览: 自适应三段式管道 —— 零转码直通 / VPU 硬编 / 软编, 不再分组轮播

实时预览为什么这么设计(2026-10-17 重构)
--------------------------------------
旧设计: 所有子码流一律 libx264 软编成 HLS。RK3399 软编单路约 81% CPU,
并发锁 4 路, 再叠加"分 2 组轮播"——结果奇数路自动播、偶数路只能手点,
点满 4 路还被上限挡住, 体验怪异且 10 路永远看不全。

新设计先回答一个问题: 真的每一路都需要"转码"吗?
  1. 子码流本来就是 H.264(逐台 ffprobe 探测, 结果缓存 6 小时):
     → 零转码直通。ffmpeg -c:v copy 把 RTSP 直接 remux 成 HLS/TS,
       单路 CPU 约 2-5%, 10 路全开也毫无压力。金利来 10 路里 6 路
       (.21-.24/.29/.30)子码流就是 H.264, 改完相机设置后全部走这条。
  2. 子码流是 HEVC(浏览器普遍不解):
     → 优先走板子 VPU 硬编(hevc_v4l2m2m 解码 + h264_v4l2m2m 编码)。
       需要较新 ffmpeg(>=5.1, 4.2.7 对 RK3399 的 multiplanar 编码节点
       支持不全会直接失败)。启动时自动探测 + 实测一条, 不可用就自动放弃。
  3. 以上都不可用:
     → 退回 libx264 软编(即旧参数), 并发 4 路。
任一路启动后进程死掉且从未产出切片, 记失败并失效该路探测缓存
(可能相机端改过编码), 不会黑屏卡死; 硬编全局失败则整体回退软编。

并发额度按模式分别计算: 直通不占软编额度(只受 MAX_TOTAL 兜底),
硬编 8 路, 软编 4 路。分组轮播整体删除——直通模式下 10 路常开
也没有 CPU 压力, 打开页面即全部实时。

路径均可用环境变量覆盖(NVR_BASE / NVR_CAMERAS_JSON / NVR_MAX_SW ...),
便于本地测试与迁移, 默认值即金利来现网配置。
"""
import os
import re
import json
import time
import shutil
import socket
import argparse
import threading
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "").strip())
    except Exception:
        return default


# ---------------- 路径 ----------------
BASE_DIR = os.environ.get("NVR_BASE", "/mnt/nvr")
REC_DIR = os.path.join(BASE_DIR, "recordings")
LOG_DIR = os.path.join(BASE_DIR, "logs")
HLS_ROOT = os.path.join(BASE_DIR, "hls")
STATE_DIR = os.path.join(BASE_DIR, "state")

# ---------------- 摄像头清单(与录制脚本保持一致) ----------------
# 金利来两批摄像头: .21-.24/.29/.30 无认证、子码流在根路径 /;
#                  .25-.28 需认证 admin/密码、子码流在 /stream2
CAMERAS_JSON = os.environ.get("NVR_CAMERAS_JSON", "").strip()
if CAMERAS_JSON:
    CAMERAS = json.loads(CAMERAS_JSON)
else:
    CAMERAS = [
        {"name": "Camera1",  "ip": "192.168.1.21", "label": "Camera 1", "zone": "入口"},
        {"name": "Camera2",  "ip": "192.168.1.22", "label": "Camera 2", "zone": "走廊"},
        {"name": "Camera3",  "ip": "192.168.1.23", "label": "Camera 3", "zone": "教室A"},
        {"name": "Camera4",  "ip": "192.168.1.24", "label": "Camera 4", "zone": "教室 B"},
        # Camera5-8 是另一厂家: 需要 RTSP 认证, 且子码流在 /stream2 而非根路径
        {"name": "Camera5",  "ip": "192.168.1.25", "label": "Camera 5", "zone": "教室 C",
         "user": "admin", "pass": "YOUR_CAM_PASSWORD", "sub": "/stream2"},
        {"name": "Camera6",  "ip": "192.168.1.26", "label": "Camera 6", "zone": "教室 D",
         "user": "admin", "pass": "YOUR_CAM_PASSWORD", "sub": "/stream2"},
        {"name": "Camera7",  "ip": "192.168.1.27", "label": "Camera 7", "zone": "创客区",
         "user": "admin", "pass": "YOUR_CAM_PASSWORD", "sub": "/stream2"},
        {"name": "Camera8",  "ip": "192.168.1.28", "label": "Camera 8", "zone": "讨论区",
         "user": "admin", "pass": "YOUR_CAM_PASSWORD", "sub": "/stream2"},
        {"name": "Camera9",  "ip": "192.168.1.29", "label": "Camera 9", "zone": "外摆台"},
        # Camera10 与 .21-.24 同款: 无认证, 子码流在根路径 /(无需 user/sub 字段)
        {"name": "Camera10", "ip": "192.168.1.30", "label": "Camera 10", "zone": "后仓"},
    ]

CAM_MAP = {c["name"]: c for c in CAMERAS}
SEG_RE = re.compile(r"^(?P<cam>[A-Za-z]+\d+)_(?P<date>\d{8})_(?P<time>\d{6})\.mp4$")

# ---------------- 预览参数 ----------------
# 模式: copy=零转码直通(仅 H.264 子码流), hw=VPU 硬编, sw=libx264 软编
MODE_COPY, MODE_HW, MODE_SW = "copy", "hw", "sw"
MODE_LABEL = {"copy": "直通", "hw": "硬编", "sw": "软编"}

MAX_SW_STREAMS = _env_int("NVR_MAX_SW", 4)    # 软编并发(RK3399 实测 4 路约 324% CPU)
MAX_HW_STREAMS = _env_int("NVR_MAX_HW", 8)    # 硬编并发(VPU 编码能力余量大)
MAX_TOTAL = _env_int("NVR_MAX_TOTAL", 12)     # 总并发兜底(含直通)

SLICE_SECS = {"copy": 2, "hw": 3, "sw": 4}          # HLS 切片秒数(越小延迟越低)
HLS_LISTS = {"copy": 8, "hw": 8, "sw": 20}          # m3u8 保留切片数
IDLE_TIMEOUTS = {"copy": 120, "hw": 90, "sw": 60}   # 无客户端后自动停(秒)

# 软编参数(与旧版一致, 实测单路约 81% CPU):
PREVIEW_FPS = 12         # 降帧是省 CPU 最有效的一招(省 28%)
PREVIEW_PRESET = "veryfast"
PREVIEW_CRF = "26"
# 硬编参数:
HW_BITRATE = "1500k"
HW_FPS = 15

PROBE_TTL = _env_int("NVR_PROBE_TTL", 6 * 3600)   # 探测结果缓存时长(秒)
PROBE_TIMEOUT = 10                                 # 单路 ffprobe 超时(RTSP 不会自己结束)

# 板子能力(启动时探测):
#   hw_enc   —— h264_v4l2m2m 编码器存在且实测可用
#   hevc_dec —— hevc_v4l2m2m 硬解码器存在(RK3399 rkvdec)
CAPS = {"hw_enc": False, "hevc_dec": False, "ffmpeg": None, "checked": False}

for _d in (HLS_ROOT, STATE_DIR, LOG_DIR):
    os.makedirs(_d, exist_ok=True)


def log(msg):
    try:
        with open(os.path.join(LOG_DIR, "live.log"), "a") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ============================================================
# 能力探测: 这台板子能不能硬编(只跑一次)
# ============================================================
def _ffmpeg_has(section, name):
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-" + section],
                           capture_output=True, text=True, timeout=15)
        return re.search(r"\b%s\b" % re.escape(name), r.stdout) is not None
    except Exception:
        return False


def _hw_encode_works():
    """实测一次硬编: lavfi 测试源 -> h264_v4l2m2m -> null。
    ffmpeg 4.2.7 对 RK3399 的 multiplanar 编码节点支持不全,
    只列在 -encoders 里但一用就报 'Could not find a valid device'。
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10:duration=1",
             "-c:v", "h264_v4l2m2m", "-f", "null", "-"],
            capture_output=True, text=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def detect_caps():
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-version"],
                           capture_output=True, text=True, timeout=15)
        m = re.search(r"ffmpeg version (\S+)", r.stdout or "")
        CAPS["ffmpeg"] = m.group(1) if m else None
    except Exception:
        pass
    CAPS["hevc_dec"] = _ffmpeg_has("decoders", "hevc_v4l2m2m")
    CAPS["hw_enc"] = _ffmpeg_has("encoders", "h264_v4l2m2m") and _hw_encode_works()
    CAPS["checked"] = True
    log("能力探测: ffmpeg=%s 硬编=%s HEVC硬解=%s" % (
        CAPS["ffmpeg"], "可用" if CAPS["hw_enc"] else "不可用",
        "有" if CAPS["hevc_dec"] else "无"))


# ============================================================
# 子码流探测: 逐台 ffprobe, 结果缓存(相机端改编码后缓存要失效)
# ============================================================
def _probe_path(name):
    return os.path.join(STATE_DIR, "probe_%s.json" % name)


def probe_substream(cam):
    """探测子码流编码/分辨率/帧率。RTSP 不会自己结束, 必须超时强杀。"""
    url = sub_url(cam)
    cmd = ["ffprobe", "-hide_banner", "-loglevel", "error"]
    if url.startswith("rtsp://"):
        cmd += ["-rtsp_transport", "tcp", "-stimeout", "5000000"]
    cmd += ["-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,avg_frame_rate",
            "-of", "json", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=PROBE_TIMEOUT)
        j = json.loads(r.stdout or "{}")
        st = (j.get("streams") or [{}])[0]
        if not st.get("codec_name"):
            return None
        return {"codec": st["codec_name"],
                "width": st.get("width"), "height": st.get("height"),
                "fps": st.get("avg_frame_rate"), "ts": int(time.time())}
    except Exception:
        return None


def get_probe(name, lazy=False):
    """取探测结果(缓存 6 小时)。lazy=True 时缓存失效就现探(会阻塞)。"""
    p = _probe_path(name)
    try:
        with open(p) as fh:
            info = json.load(fh)
        if time.time() - info.get("ts", 0) < PROBE_TTL:
            return info
    except Exception:
        pass
    if not lazy:
        return None
    info = probe_substream(CAM_MAP[name])
    if info:
        save_probe(name, info)
    return info


def save_probe(name, info):
    try:
        with open(_probe_path(name), "w") as fh:
            json.dump(info, fh)
    except Exception:
        pass


def invalidate_probe(name):
    try:
        os.remove(_probe_path(name))
    except OSError:
        pass


def choose_mode(cam, info):
    """按探测结果选模式。没有探测结果时保守用软编(旧行为)。"""
    if not info:
        return MODE_SW
    codec = (info.get("codec") or "").lower()
    if codec == "h264":
        return MODE_COPY
    if codec == "hevc" and CAPS["hw_enc"] and CAPS["hevc_dec"]:
        return MODE_HW
    return MODE_SW


# ============================================================
# 实时预览: 每路一个按需启动 / 空闲自停的 remux 或转码进程
# ============================================================
class LiveStream:
    def __init__(self, cam):
        self.cam = cam
        self.name = cam["name"]
        self.dir = os.path.join(HLS_ROOT, self.name)
        self.proc = None
        self.lock = threading.Lock()
        self.last_used = time.time()
        self.starting = False
        self.mode = None            # 本次启动使用的模式
        self.started_at = 0.0
        self.ever_ready = False     # 是否曾经产出过切片
        self.fail_marks = 0         # 连续启动失败次数(日志限流用)

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _slot_ok(self, mode):
        if active_count() >= MAX_TOTAL:
            return False
        if mode == MODE_SW and mode_count(MODE_SW) >= MAX_SW_STREAMS:
            return False
        if mode == MODE_HW and mode_count(MODE_HW) >= MAX_HW_STREAMS:
            return False
        return True

    def start(self):
        """启动(或已在启动中)返回 True; 因并发名额被拒返回 False。"""
        with self.lock:
            if self.running or self.starting:
                return True
            # 现探(缓存过期或从未探测过); 探测失败按软编走, 不影响旧体验
            info = get_probe(self.name, lazy=True)
            mode = choose_mode(self.cam, info)
            if not self._slot_ok(mode):
                log("并发已满(软编 %d/%d, 总 %d/%d), 暂不启动 %s" % (
                    mode_count(MODE_SW), MAX_SW_STREAMS,
                    active_count(), MAX_TOTAL, self.name))
                return False
            self.starting = True
            try:
                shutil.rmtree(self.dir, ignore_errors=True)
                os.makedirs(self.dir, exist_ok=True)
                m3u8 = os.path.join(self.dir, "index.m3u8")
                cmd = build_cmd(self.cam, mode, self.dir, m3u8)
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL,
                    stderr=open(os.path.join(LOG_DIR, "live_%s.log" % self.name), "a"))
                self.mode = mode
                self.started_at = time.time()
                self.ever_ready = False
                log("启动实时%s %s (%s, 子码流 %s)" % (
                    MODE_LABEL.get(mode, mode), self.name, self.cam["ip"],
                    (info or {}).get("codec", "未知")))
            except Exception as e:
                log("启动失败 %s: %s" % (self.name, e))
                return False
            finally:
                self.starting = False
            return True

    def stop(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                log("停止实时转码 %s" % self.name)
            self.proc = None

    def touch(self):
        # 不用"客户端计数"判定活跃: 浏览器会持续请求切片, 计数只增不减,
        # 页面关掉后计数仍挂着, 导致永不释放。这里只记最后访问时间。
        with self.lock:
            self.last_used = time.time()

    def untouch(self):
        with self.lock:
            self.last_used = 0

    def idle(self):
        with self.lock:
            t = IDLE_TIMEOUTS.get(self.mode, 60)
            return (time.time() - self.last_used) > t

    def playlist_ready(self):
        """m3u8 已有内容(至少一个切片写完)才算就绪"""
        p = os.path.join(self.dir, "index.m3u8")
        if not os.path.isfile(p):
            return False
        try:
            ok = os.path.getsize(p) > 20
        except OSError:
            return False
        if ok:
            self.ever_ready = True
        return ok


def build_cmd(cam, mode, out_dir, m3u8):
    """按模式构造 ffmpeg 命令。"""
    url = sub_url(cam)
    tail = [
        "-an", "-sn", "-dn",
        "-f", "hls",
        "-hls_time", str(SLICE_SECS.get(mode, 4)),
        "-hls_list_size", str(HLS_LISTS.get(mode, 20)),
        # program_date_time: 让播放器能做精确的时间轴对齐,
        #   omit_endlist: 直播流必须带, 否则播放器会认为流已结束而停住
        "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments+program_date_time",
        "-hls_segment_filename", os.path.join(out_dir, "seg_%06d.ts"),
        m3u8,
    ]
    head = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
    if url.startswith("rtsp://"):
        head += ["-rtsp_transport", "tcp", "-stimeout", "10000000"]
    head += ["-i", url]
    if mode == MODE_COPY:
        # 子码流本来就是 H.264: 直接 remux, 不重编码, 单路 CPU 约 2-5%
        # 输入已是目标尺寸与帧率, 不加 -r / -vf scale, 一点不多花 CPU
        return head + ["-map", "0:v:0", "-c:v", "copy"] + tail
    if mode == MODE_HW:
        # HEVC 子码流: rkvdec 硬解 + VPU 硬编 H.264
        hw_head = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
                   "-c:v", "hevc_v4l2m2m"]
        if url.startswith("rtsp://"):
            hw_head += ["-rtsp_transport", "tcp", "-stimeout", "10000000"]
        return hw_head + ["-i", url,
                          "-map", "0:v:0",
                          "-c:v", "h264_v4l2m2m",
                          "-b:v", HW_BITRATE, "-g", "40", "-bf", "0",
                          "-r", str(HW_FPS), "-pix_fmt", "yuv420p"] + tail
    # 软编(旧参数, 单路约 81% CPU)
    return head + ["-map", "0:v:0",
                   "-r", str(PREVIEW_FPS),
                   "-c:v", "libx264", "-preset", PREVIEW_PRESET,
                   "-tune", "zerolatency",
                   "-profile:v", "baseline", "-level", "3.1",
                   "-pix_fmt", "yuv420p",
                   "-crf", PREVIEW_CRF, "-bf", "0", "-g", "50"] + tail


LIVE = {c["name"]: LiveStream(c) for c in CAMERAS}


def active_count():
    return sum(1 for s in LIVE.values() if s.running)


def mode_count(mode):
    return sum(1 for s in LIVE.values() if s.running and s.mode == mode)


# ============================================================
# 守护: 空闲回收 + 失败处理(硬编整体回退 / 直通重新探测)
# ============================================================
def reaper_loop():
    # 这个守护负责回收空闲转码, 挂了就再也不会释放 CPU,
    # 所以异常必须让进程退出并被 systemd 重启, 而不是静默死掉。
    while True:
        time.sleep(10)
        try:
            for s in LIVE.values():
                if s.running:
                    if s.idle():
                        log("空闲超时, 关闭 %s" % s.name)
                        s.stop()
                    continue
                # 进程已退出: 从未产出过切片才算"启动失败"
                # (相机离线 / 子码流编码不符 / 硬编挂死)
                if s.proc is not None and not s.ever_ready:
                    s.fail_marks += 1
                    # 离线摄像头会反复触发, 日志限流: 前 3 次 + 之后每 10 次记一条
                    if s.fail_marks <= 3 or s.fail_marks % 10 == 0:
                        log("%s 启动失败(从未产出切片, 第 %d 次, 模式=%s)" % (
                            s.name, s.fail_marks, s.mode))
                    if s.mode == MODE_HW:
                        CAPS["hw_enc"] = False   # 硬编全局不可信, 回退软编
                        log("硬编链路异常, 已全局回退软编")
                    elif s.mode == MODE_COPY and s.fail_marks <= 3:
                        # 可能相机端改过编码(如 H.264->H.265),
                        # 失效缓存下次重新探测(同样限流, 避免离线摄像头反复重探)
                        invalidate_probe(s.name)
                        log("直通失败, 已失效 %s 探测缓存, 下次启动重新探测" % s.name)
                s.proc = None
        except Exception as e:
            log("reaper 异常: %s" % e)


# ============================================================
# 启动: 能力探测 -> 逐路探测子码流 -> 预热直通流
# ============================================================
def init_worker():
    detect_caps()
    for c in CAMERAS:
        if not CAPS["checked"]:
            break
        info = probe_substream(c)
        if info:
            save_probe(c["name"], info)
        time.sleep(1.0)   # 错开探测, 避免同时开太多 RTSP 连接
    modes = {}
    for c in CAMERAS:
        m = choose_mode(c, get_probe(c["name"]))
        modes[c["name"]] = m
    log("子码流探测完成: " + ", ".join(
        "%s=%s" % (n, MODE_LABEL.get(m, m)) for n, m in modes.items()))
    # 直通几乎不花 CPU, 启动即预热, 打开页面无需等待
    for c in CAMERAS:
        if modes.get(c["name"]) == MODE_COPY:
            LIVE[c["name"]].start()


def sub_url(cam):
    """按摄像头构造子码流 RTSP URL。
    两批摄像头厂家不同:
      .21-.24  无认证, 子码流在根路径 /
      .25-.28  需认证 admin/密码, 子码流在 /stream2
    带 src 字段时直接用给定 URL(测试或特殊源)。
    """
    if cam.get("src"):
        return cam["src"]
    path = cam.get("sub", "/")
    if cam.get("user"):
        return "rtsp://%s:%s@%s:554%s" % (
            cam["user"], cam.get("pass", ""), cam["ip"], path)
    return "rtsp://%s:554%s" % (cam["ip"], path)


def probe_alive(ip):
    try:
        s = socket.create_connection((ip, 554), timeout=1.2)
        s.close()
        return True
    except Exception:
        return False


# ============================================================
# 系统资源采集: 全部走/proc 直读, 不 fork 进程(这个接口每秒都会被调)
# ============================================================
def _readf(p, default=0.0):
    try:
        with open(p) as fh:
            return float(fh.read().strip())
    except Exception:
        return default


def cpu_times():
    """返回 [user, nice, system, idle, iowait, irq, softirq, steal]"""
    try:
        with open("/proc/stat") as fh:
            f = fh.readline().split()
        return [float(x) for x in f[1:9]]
    except Exception:
        return None


def mem_info():
    try:
        with open("/proc/meminfo") as fh:
            d = {}
            for line in fh:
                k, _, v = line.partition(":")
                d[k] = float(v.split()[0]) * 1024
        total = d.get("MemTotal", 0)
        avail = d.get("MemAvailable", d.get("MemFree", 0))
        used = total - avail
        return {
            "total_gb": round(total / 1024**3, 2),
            "used_gb": round(used / 1024**3, 2),
            "avail_gb": round(avail / 1024**3, 2),
            "used_pct": round(used * 100 / total, 1) if total else 0,
        }
    except Exception:
        return {}


def temp_info():
    """读 SoC 温度, 返回最高的一路"""
    best = None
    import glob
    for z in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        t = _readf(z, -1)
        if t < 0:
            continue
        t = t / 1000.0 if t > 1000 else t
        name = z.split("thermal_zone")[-1].split("/")[0]
        if best is None or t > best["c"]:
            best = {"c": round(t, 1), "zone": name}
    return best


def net_rate():
    """网卡速率, 存上次采样值做差"""
    rx = _readf("/sys/class/net/eth0/statistics/rx_bytes", 0)
    tx = _readf("/sys/class/net/eth0/statistics/tx_bytes", 0)
    now = time.time()
    prev = getattr(net_rate, "_prev", None)
    net_rate._prev = (rx, tx, now)
    if not prev:
        return {"rx_kbps": 0, "tx_kbps": 0}
    prx, ptx, pnow = prev
    dt = now - pnow
    if dt <= 0:
        return {"rx_kbps": 0, "tx_kbps": 0}
    return {
        "rx_kbps": int(max(0, rx - prx) * 8 / dt / 1000),
        "tx_kbps": int(max(0, tx - ptx) * 8 / dt / 1000),
    }


def sys_info():
    """一次采完所有指标, 单个 API 给前端"""
    ct = cpu_times()
    cpu_pct = None
    if ct:
        prev = getattr(sys_info, "_prev_cpu", None)
        sys_info._prev_cpu = (ct, time.time())
        if prev:
            p, pnow = prev
            dt = sum(ct) - sum(p)
            idle = (ct[3] - p[3]) + (ct[4] - p[4])
            if dt > 0:
                cpu_pct = round(max(0.0, min(100.0, (dt - idle) * 100 / dt)), 1)
    try:
        with open("/proc/loadavg") as fh:
            la = fh.read().split()[:3]
        load1, load5, load15 = [float(x) for x in la]
    except Exception:
        load1 = load5 = load15 = 0.0
    try:
        with open("/proc/uptime") as fh:
            up = float(fh.read().split()[0])
    except Exception:
        up = 0
    ncpu = os.cpu_count() or 1
    d = disk_info()
    return {
        "cpu_pct": cpu_pct,
        "ncpu": ncpu,
        "load1": round(load1, 2),
        "load5": round(load5, 2),
        "load15": round(load15, 2),
        "load_per_core": round(load1 / ncpu, 2),
        "mem": mem_info(),
        "temp": temp_info(),
        "net": net_rate(),
        "uptime_s": int(up),
        "disk": d,
        "streams": active_count(),
        "max_streams": MAX_SW_STREAMS,
        "stream_modes": {"copy": mode_count(MODE_COPY),
                         "hw": mode_count(MODE_HW),
                         "sw": mode_count(MODE_SW)},
        "caps": {"hw_enc": CAPS["hw_enc"], "ffmpeg": CAPS["ffmpeg"]},
    }


def disk_info():
    st = os.statvfs(BASE_DIR)
    total = st.f_blocks * st.f_frsize
    free = st.f_bfree * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    used = total - free
    return {
        "total_gb": round(total / 1024**3, 1),
        "used_gb": round(used / 1024**3, 1),
        "free_gb": round(avail / 1024**3, 1),
        "used_pct": round(used * 100 / total, 1) if total else 0,
    }


def cam_status(name):
    """该摄像头是否有正在写入的分片(录制中)"""
    d = os.path.join(REC_DIR, name)
    if not os.path.isdir(d):
        return "offline"
    files = [f for f in os.listdir(d) if f.endswith(".mp4")]
    if not files:
        return "offline"
    newest = max(os.path.getmtime(os.path.join(d, f)) for f in files)
    if time.time() - newest < 90:
        return "recording"
    return "standby"


def list_segments(cam, date=None):
    """列出某摄像头(可选某天)的分片"""
    d = os.path.join(REC_DIR, cam)
    if not os.path.isdir(d):
        return []
    rows = []
    for f in os.listdir(d):
        m = SEG_RE.match(f)
        if not m:
            continue
        if m.group("cam") != cam:
            continue
        if date and m.group("date") != date:
            continue
        p = os.path.join(d, f)
        try:
            stt = os.stat(p)
        except OSError:
            continue
        rows.append((f, m, stt))
    rows.sort(key=lambda r: r[2].st_mtime)
    newest_name = rows[-1][0] if rows else None

    out = []
    for f, m, stt in rows:
        out.append({
            "file": f,
            "date": m.group("date"),
            "time": m.group("time"),
            "size_mb": round(stt.st_size / 1024**2, 1),
            "mtime": int(stt.st_mtime),
            "ts": int(stt.st_mtime),
            "live": f == newest_name and (time.time() - stt.st_mtime) < 3,
        })
    return out


def available_dates(cam):
    d = os.path.join(REC_DIR, cam)
    if not os.path.isdir(d):
        return []
    return sorted({SEG_RE.match(f).group("date")
                   for f in os.listdir(d) if SEG_RE.match(f)}, reverse=True)


def read_overview():
    """总览: 每路摄像头状态 + 今日分片数 + 今日体积"""
    today = time.strftime("%Y%m%d")
    cams = []
    for c in CAMERAS:
        segs = list_segments(c["name"])
        today_segs = [s for s in segs if s["date"] == today]
        cams.append({
            **c,
            "status": cam_status(c["name"]),
            "total_segments": len(segs),
            "today_segments": len(today_segs),
            "today_mb": round(sum(s["size_mb"] for s in today_segs), 1),
            "dates": available_dates(c["name"])[:30],
        })
    return {"cameras": cams, "disk": disk_info(),
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S")}


def probe_file(path):
    """取视频宽高/时长/编码"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,codec_name",
             "-show_entries", "format=duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=15)
        j = json.loads(r.stdout or "{}")
        st = (j.get("streams") or [{}])[0]
        return {
            "width": st.get("width"), "height": st.get("height"),
            "codec": st.get("codec_name"),
            "duration": round(float(j.get("format", {}).get("duration", 0)), 1),
        }
    except Exception:
        return {}


def live_api():
    cams = []
    for c in CAMERAS:
        s = LIVE[c["name"]]
        info = get_probe(c["name"])
        cams.append({
            **c,
            "streaming": s.running,
            "mode": s.mode if s.running else choose_mode(c, info),
            "codec": (info or {}).get("codec"),
            "clients": 0,
            "ready": s.playlist_ready(),
        })
    return {
        "cameras": cams,
        "active_streams": active_count(),
        "stream_modes": {"copy": mode_count(MODE_COPY),
                         "hw": mode_count(MODE_HW),
                         "sw": mode_count(MODE_SW)},
        "max_streams": MAX_SW_STREAMS,
        "max_hw_streams": MAX_HW_STREAMS,
        "max_total": MAX_TOTAL,
        "slice_sec": SLICE_SECS["sw"],
        "caps": {"hw_enc": CAPS["hw_enc"], "ffmpeg": CAPS["ffmpeg"]},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "NVR/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, code=400):
        self._json({"error": msg}, code)

    def _send_file(self, p, ctype, cache="no-store, no-cache, must-revalidate"):
        if not os.path.isfile(p):
            return self._err("文件不存在", 404)
        size = os.path.getsize(p)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        with open(p, "rb") as fh:
            self.wfile.write(fh.read())

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            # ---------- 实时预览 ----------
            if u.path == "/live":
                cam = q.get("cam", [""])[0]
                if cam not in CAM_MAP:
                    return self._err("未知摄像头: %s" % cam)
                s = LIVE[cam]
                s.touch()
                if not s.start() and not s.running:
                    # 名额已满: 明确告诉前端"是排队不是故障", 前端会自动重试
                    return self._json({
                        "status": "busy", "cam": cam,
                        "reason": "转码名额已满(软编 %d/%d)" % (
                            mode_count(MODE_SW), MAX_SW_STREAMS)}, 429)
                if not s.playlist_ready():
                    return self._json({"status": "starting", "cam": cam}, 202)
                # 必须把 m3u8 里的相对切片名改成绝对路径。
                with open(os.path.join(s.dir, "index.m3u8")) as fh:
                    txt = fh.read()
                txt = re.sub(r"(?m)^(seg_\d+\.ts)$",
                             r"/hls/%s/\1" % cam, txt)
                body = txt.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/x-mpegURL")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, no-cache")
                self.end_headers()
                self.wfile.write(body)
                return

            if u.path.startswith("/hls/"):
                parts = [p for p in u.path.split("/") if p]
                if len(parts) != 3:
                    return self._err("路径错误", 404)
                cam, fn = parts[1], parts[2]
                if cam not in CAM_MAP:
                    return self._err("未知摄像头", 404)
                if not re.match(r"^seg_\d+\.ts$", fn):
                    return self._err("非法切片名", 400)
                LIVE[cam].touch()
                return self._send_file(os.path.join(HLS_ROOT, cam, fn),
                                       "video/mp2t")

            if u.path == "/live/stop":
                cam = q.get("cam", [""])[0]
                if cam in LIVE:
                    if q.get("force", ["0"])[0] == "1":
                        LIVE[cam].last_used = 0
                        LIVE[cam].stop()
                    else:
                        LIVE[cam].untouch()
                return self._json({"ok": True})

            if u.path == "/api/live":
                return self._json(live_api())

            # 相机端改过编码后调用: 失效探测缓存 + 停掉在放的流, 下次自动重新探测
            if u.path == "/api/reprobe":
                def _job():
                    for c in CAMERAS:
                        invalidate_probe(c["name"])
                        LIVE[c["name"]].stop()
                        info = probe_substream(c)
                        if info:
                            save_probe(c["name"], info)
                        time.sleep(1.0)
                    log("手动重新探测完成")
                threading.Thread(target=_job, daemon=True).start()
                return self._json({"ok": True, "msg": "已在后台重新探测全部子码流"})

            if u.path == "/api/ping":
                return self._json({"cameras": [
                    {"name": c["name"], "ip": c["ip"],
                     "alive": probe_alive(c["ip"])} for c in CAMERAS]})

            if u.path == "/api/sysinfo":
                return self._json(sys_info())

            # ---------- 静态资源 ----------
            if u.path.startswith("/static/"):
                fn = os.path.basename(u.path)
                if fn not in ("hls.min.js", "logo-light.png", "favicon-32.png",
                              "favicon-64.png", "favicon-512.png",
                              "apple-touch-icon.png"):
                    return self._err("Not Found", 404)
                p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "static", fn)
                if not os.path.isfile(p):
                    return self._err("资源未部署: %s" % fn, 404)
                ctype = ("application/javascript; charset=utf-8"
                         if fn.endswith(".js") else "image/png")
                return self._send_file(p, ctype, "public, max-age=86400")

            # ---------- 录像查询 ----------
            if u.path == "/api/overview":
                return self._json(read_overview())

            if u.path == "/api/segments":
                cam = q.get("cam", [""])[0]
                date = q.get("date", [None])[0]
                if cam not in CAM_MAP:
                    return self._err("未知摄像头: %s" % cam)
                segs = list_segments(cam, date)
                want = q.get("probe", ["0"])[0] == "1" and len(segs) <= 8
                if want:
                    for s in segs:
                        s["probe"] = probe_file(
                            os.path.join(REC_DIR, cam, s["file"]))
                return self._json({
                    "camera": cam,
                    "date": date,
                    "segments": segs,
                    "dates": available_dates(cam),
                })

            if u.path == "/api/probe":
                cam = q.get("cam", [""])[0]
                f = q.get("file", [""])[0]
                p = os.path.join(REC_DIR, cam, os.path.basename(f))
                if cam not in CAM_MAP or not os.path.isfile(p):
                    return self._err("文件不存在")
                return self._json(probe_file(p))

            if u.path == "/api/status":
                return self._json({
                    "cameras": {c["name"]: cam_status(c["name"]) for c in CAMERAS},
                    "disk": disk_info(),
                })

            if u.path in ("/", "/index.html"):
                p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
                if not os.path.isfile(p):
                    return self._err("index.html 未部署", 404)
                with open(p, "rb") as fh:
                    body = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if u.path == "/video" or u.path == "/download":
                cam = q.get("cam", [""])[0]
                f = os.path.basename(q.get("file", [""])[0])
                p = os.path.join(REC_DIR, cam, f)
                if cam not in CAM_MAP or not os.path.isfile(p) \
                        or os.path.dirname(os.path.realpath(p)) != \
                        os.path.realpath(os.path.join(REC_DIR, cam)):
                    return self._err("文件不存在", 404)

                d = os.path.join(REC_DIR, cam)
                peers = [(x, os.stat(os.path.join(d, x)).st_mtime)
                         for x in os.listdir(d)
                         if SEG_RE.match(x) and SEG_RE.match(x).group("cam") == cam
                         and os.path.isfile(os.path.join(d, x))]
                if peers:
                    newest = max(peers, key=lambda r: r[1])
                    if newest[0] == f and (time.time() - newest[1]) < 3:
                        return self._err("该片段正在录制中, 请稍候(约 1 分钟内闭合)", 409)

                size = os.path.getsize(p)
                start = self.headers.get("Range")
                dl = (u.path == "/download")
                ctype = "video/mp4"
                cd = f'attachment; filename="{f}"' if dl else "inline"

                if start:
                    m = re.match(r"bytes=(\d*)-(\d*)", start)
                    if m:
                        s = int(m.group(1) or 0)
                        e = int(m.group(2)) if m.group(2) else size - 1
                        if s >= size:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{size}")
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                        e = min(e, size - 1)
                        length = e - s + 1
                        self.send_response(206)
                        self.send_header("Content-Type", ctype)
                        self.send_header("Content-Length", str(length))
                        self.send_header("Content-Range", f"bytes {s}-{e}/{size}")
                        self.send_header("Accept-Ranges", "bytes")
                        if dl:
                            self.send_header("Content-Disposition", cd)
                        self.end_headers()
                        with open(p, "rb") as fh:
                            fh.seek(s)
                            remaining = length
                            while remaining > 0:
                                chunk = fh.read(min(65536, remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                        return

                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Disposition", cd)
                self.end_headers()
                with open(p, "rb") as fh:
                    while True:
                        chunk = fh.read(262144)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return

            return self._err("Not Found", 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            return self._err("服务器内部错误: %s" % e, 500)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    sys_info()   # 先采基线, 否则首次请求拿不到 CPU 占用
    threading.Thread(target=reaper_loop, daemon=True).start()
    threading.Thread(target=init_worker, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    log("服务启动 :%d (直通不占额度, 硬编上限 %d, 软编上限 %d)" % (
        a.port, MAX_HW_STREAMS, MAX_SW_STREAMS))
    print("NVR service on http://%s:%d" % (a.host, a.port))
    srv.serve_forever()


if __name__ == "__main__":
    main()
