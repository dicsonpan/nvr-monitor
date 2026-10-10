#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NVR 监控服务 (示例配置)
- 录像查询: 扫描 /mnt/nvr/recordings 下的 10 分钟分片, 时间轴检索 / 在线播放 / 下载
- 实时预览: RTSP 子码流 -> HLS 切片, 浏览器多路九宫格

实时预览为什么这么设计
----------------------
浏览器不能直接播 RTSP。HLS 相比 WebRTC/MSE 的优势是纯 HTTP、穿透性好、
抗网络抖动(切片后抖动不会让整段卡死), 延迟 1-2 秒对安防足够。

按需转码 + 限流的原因
---------------------
RK3399 的 VPU 硬编不可用: /dev/video2 是 Multiplanar 类型,
ffmpeg 4.2.7(focal 最新版) 的 h264_v4l2m2m 对它支持不全, 实测直接挂死。
故只能用软编, 实测单路 704x576 约需 128% CPU, 4 路约 565%(6 核已近上限)。
因此: 只转"当前正在看"的摄像头, 全局并发上限 MAX_STREAMS, 空闲 60 秒自动停。
录像本身是零编码, 不受此影响。

画面比例
--------
子码流 704x576 = 1.222:1 (既不是 4:3 也不是 16:9)。缩放保持原始比例,
前端 object-fit: contain + 黑边留白, 不裁切不变形。
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

REC_DIR = "/mnt/nvr/recordings"
LOG_DIR = "/mnt/nvr/logs"
BASE_DIR = "/mnt/nvr"
HLS_ROOT = "/mnt/nvr/hls"

# 实时预览参数
#
# CPU 实测(RK3399 软编, 单路 800x448 输入):
#   ultrafast crf23(旧)      112%   <- 4 路 = 448%
#   veryfast  crf26          92%
#   veryfast  crf26 + 12fps   81%   <- 当前采用, 4 路 = 324%
#   640x360 + 12fps           70%
# 监控画面不需要高帧率, 12fps 完全够看(动作略糊但静止清晰)。
# 配合 MAX_STREAMS=4, 4 路预览约占 324%, 加上 10 路零转码录制也还有余量。
MAX_STREAMS = 4          # 全局同时转码路数上限(留CPU 余量, 不能再高)
SLICE_SEC = 4            # 每个 HLS 切片秒数(太小会让切片滚动过快, 播放器追不上而黑屏)
PREVIEW_W, PREVIEW_H = 800, 448   # 子码流实际分辨率(2026-10-03 相机端已改)
# 子码流原生就是 16:9 (800x448 = 1.7857, 与 16:9 差 0.45%)。
# 输入已等于目标尺寸, **不要加 scale 滤镜** —— 那会白白吃掉 CPU。
# 万一将来相机又改成别的分辨率, 去掉 scale 也只是保留原始尺寸, 不会变形。
PREVIEW_FPS = 12         # 降帧是省 CPU 最有效的一招(省 28%)
PREVIEW_PRESET = "veryfast"   # ultrafast 虽然更快但画质差且并不省 CPU(实测 112% vs 92%)
PREVIEW_CRF = "26"       # 画质/体积平衡点, 监控场景 crf23->26 肉眼几乎无差别
HLS_LIST = 20            # m3u8 保留切片数 (20 x 4s = 80 秒缓冲, 足够浏览器追赶)
IDLE_TIMEOUT = 60        # 无客户端后自动停止转码

os.makedirs(HLS_ROOT, exist_ok=True)

# 摄像头定义(与录制脚本保持一致)
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


def log(msg):
    try:
        with open(os.path.join(LOG_DIR, "live.log"), "a") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ============================================================
# 实时预览: 每路一个按需启动 / 空闲自停的转码进程
# ============================================================
class LiveStream:
    def __init__(self, cam):
        self.cam = cam
        self.name = cam["name"]
        self.dir = os.path.join(HLS_ROOT, self.name)
        self.proc = None
        self.lock = threading.Lock()
        self.clients = 0
        self.last_used = time.time()
        self.starting = False
        self.ever_started = False

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        with self.lock:
            if self.running or self.starting:
                return
            if active_count() >= MAX_STREAMS:
                log("并发已满(%d), 暂不启动 %s" % (MAX_STREAMS, self.name))
                return
            self.starting = True
            try:
                shutil.rmtree(self.dir, ignore_errors=True)
                os.makedirs(self.dir, exist_ok=True)
                m3u8 = os.path.join(self.dir, "index.m3u8")
                cmd = [
                    "ffmpeg", "-hide_banner", "-loglevel", "warning",
                    "-rtsp_transport", "tcp",
                    "-stimeout", "10000000",
                    "-i", sub_url(self.cam),   # 认证与路径由 sub_url 按厂家处理
                    "-an", "-sn", "-dn",
                    # 不加 -vf scale: 输入已是 800x448, 缩放只会白吃 CPU
                    # 降帧 + 降 crf + 换 preset, 三者叠加省掉约 28% CPU(实测 112% -> 81%)
                    "-r", str(PREVIEW_FPS),
                    "-c:v", "libx264", "-preset", PREVIEW_PRESET,
                    "-tune", "zerolatency",
                    "-profile:v", "baseline", "-level", "3.1",
                    "-pix_fmt", "yuv420p",
                    "-crf", PREVIEW_CRF, "-bf", "0", "-g", "50",
                    "-f", "hls",
                    "-hls_time", str(SLICE_SEC),
                    "-hls_list_size", str(HLS_LIST),
                    # program_date_time: 让播放器能做精确的时间轴对齐,
                    #   缺了它 hls.js 只能靠序号推算, 遇到切片滚动就会反复重同步失败(黑屏)
                    # omit_endlist: 直播流必须带, 否则播放器会认为流已结束而停住
                    "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments+program_date_time",
                    "-hls_segment_filename", os.path.join(self.dir, "seg_%06d.ts"),
                    m3u8,
                ]
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL,
                    stderr=open(os.path.join(LOG_DIR, "live_%s.log" % self.name), "a"))
                self.ever_started = True
                log("启动实时转码 %s (%s)" % (self.name, self.cam["ip"]))
            except Exception as e:
                log("启动失败 %s: %s" % (self.name, e))
            finally:
                self.starting = False

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
        # 兼容前端调用: 立即判定为空闲, 由 reaper 回收
        with self.lock:
            self.last_used = 0

    def idle(self):
        with self.lock:
            return (time.time() - self.last_used) > IDLE_TIMEOUT

    def playlist_ready(self):
        """m3u8 已有内容(至少一个切片写完)才算就绪"""
        p = os.path.join(self.dir, "index.m3u8")
        if not os.path.isfile(p):
            return False
        try:
            return os.path.getsize(p) > 20
        except OSError:
            return False


LIVE = {c["name"]: LiveStream(c) for c in CAMERAS}


def active_count():
    return sum(1 for s in LIVE.values() if s.running)


# ---------- 分组轮转调度 ----------
#
# 背景: 板子软编 4 路预览就占约 324% CPU(600% 上限), 10 路不可能同时开。
# 但"分 2 组轮流"本身并不省 CPU —— 10 路都在看时总转码量不变。
# 真正的价值在于:
#   1. 用户滚动浏览九宫格时, 后面没启动的组会自动顶上, 不用手动点
#   2. 停留在一组时, 另一组完全空闲, CPU 只服务当前关注的 4 路
#
# 所以这里做的不是"定时强转", 而是"空闲时轮换"——
# 有客户端观看的组优先, 只有当所有组都空闲时才切换。
PREVIEW_GROUPS = 2       # 10 路分几组轮转
GROUP_SEC = 45# 每组至少停留多久(太短会让人觉得画面在闪)

# 每组一次能放几路 = MAX_STREAMS
_group_idx = {"cur": 0, "at": 0.0}


def group_of_name(name):
    """按摄像头名字判断属于第几组(0 起)。
    ⚠️ LIVE 是 {name: LiveStream} 的字典, 键是名字不是索引 ——
       曾经写成 group_of(i) 传进 name 当模数, 导致 reaper 抛
       "not all arguments converted during string formatting" 而整个挂掉。
    """
    for i, c in enumerate(CAMERAS):
        if c["name"] == name:
            return i % PREVIEW_GROUPS
    return 0


def rotate_if_idle():
    """所有组都没有活跃客户端时, 轮转到下一组。
    有客户端在看的那一组不会被抢 —— 那是用户真正关心的画面。
    """
    cur = _group_idx["cur"]
    busy_groups = set()
    for name, s in LIVE.items():
        if s.running and not s.idle():
            busy_groups.add(group_of_name(name))
    if busy_groups:
        # 有人在看, 就不动
        if cur not in busy_groups:
            # 让当前组对齐到有人在看的那组, 保证 MAX_STREAMS 额度不被空转占用
            _group_idx["cur"] = min(busy_groups)
        return
    nxt = (cur + 1) % PREVIEW_GROUPS
    if nxt == 0 and time.time() - _group_idx["at"] < GROUP_SEC:
        return
    _group_idx["cur"] = nxt
    _group_idx["at"] = time.time()
    names = [c["name"] for i, c in enumerate(CAMERAS)
             if i % PREVIEW_GROUPS == nxt]
    log("轮转到第 %d 组: %s" % (nxt + 1, ", ".join(names)))


def reaper_loop():
    # 这个守护负责回收空闲转码, 挂了就再也不会释放 CPU,
    # 所以异常必须让进程退出并被 systemd 重启, 而不是静默死掉。
    while True:
        time.sleep(10)
        for s in LIVE.values():
            if s.running and s.idle():
                log("空闲超时, 关闭 %s" % s.name)
                s.stop()
        rotate_if_idle()


def sub_url(cam):
    """按摄像头构造子码流 RTSP URL。
    两批摄像头厂家不同:
      .21-.24  无认证, 子码流在根路径 /
      .25-.28  需认证 admin/YOUR_CAM_PASSWORD, 子码流在 /stream2
    """
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
        # 缓存+回收 buffer 算"已用", 与 free 命令一致
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
        t = t / 1000.0 if t > 1000 else t   # 有些驱动已经是摄氏度
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
            # /proc/stat 各字段含义: 0 user 1 nice 2 system 3 idle 4 iowait
            # 5 irq 6 softirq 7 steal。总计 = 全部之和, 空闲 = idle + iowait
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
        "cpu_pct": cpu_pct,                 # None = 首次采样, 下一帧才有
        "ncpu": ncpu,
        "load1": round(load1, 2),
        "load5": round(load5, 2),
        "load_per_core": round(load1 / ncpu, 2),
        "mem": mem_info(),
        "temp": temp_info(),
        "net": net_rate(),
        "uptime_s": int(up),
        "disk": d,
        "streams": active_count(),
        "max_streams": MAX_STREAMS,
    }


def disk_info():
    st = os.statvfs(BASE_DIR)
    # 与 df 的 Use% 保持一致的口径:
    #   used = total - f_bfree  (f_bfree 才是"已用", f_bavail 是"普通用户可用")
    #   展示剩余空间用 f_bavail (root 保留块用户看不到, 不能算进去)
    total = st.f_blocks * st.f_frsize
    free = st.f_bfree * st.f_frsize          # 真正空闲
    avail = st.f_bavail * st.f_frsize        # 用户可用
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
    # 分片 mtime 在 90 秒内 => 正在录
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
    # 该路最新的分片 = 唯一可能正在写入的那个
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
            # 正在写入的分片: mp4 的 moov 尚未落盘, 播放器打不开。
            # 不能只看"距今多久" —— 刚闭合的分片 mtime 也很新, 会被误判。
            # 正确判据: 该路最新的分片, 且仍在增长。
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


class Handler(BaseHTTPRequestHandler):
    server_version = "NVR/1.0"

    def log_message(self, fmt, *args):
        # 静默常规访问日志, 只记录错误
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
        # HLS 切片持续追加/删除, 必须 no-store; 静态资源则可长缓存
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
                s.start()
                if not s.playlist_ready():
                    return self._json({"status": "starting", "cam": cam}, 202)
                # 必须把 m3u8 里的相对切片名改成绝对路径。
                # ffmpeg 写的是 "seg_000006.ts", 播放器会以 m3u8 的 URL(/live?cam=X)
                # 为基准解析, 结果请求到根路径 /seg_000006.ts -> 404 -> 一直黑屏。
                with open(os.path.join(s.dir, "index.m3u8")) as fh:
                    txt = fh.read()
                txt = re.sub(r"(?m)^(seg_\d+\.ts)$",
                             r"/hls/%s/\1" % cam, txt)
                body = txt.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type",
                                 "application/x-mpegURL")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, no-cache")
                self.end_headers()
                self.wfile.write(body)
                return

            if u.path.startswith("/hls/"):
                parts = [p for p in u.path.split("/") if p]
                # /hls/<cam>/seg_000001.ts
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
                        # 强制立即关闭, 不等空闲超时
                        LIVE[cam].last_used = 0
                        LIVE[cam].stop()
                    else:
                        LIVE[cam].untouch()
                return self._json({"ok": True})

            if u.path == "/api/live":
                return self._json({
                    "cameras": [{
                        **c,
                        "streaming": LIVE[c["name"]].running,
                        "clients": LIVE[c["name"]].clients,
                        "ready": LIVE[c["name"]].playlist_ready(),
                    } for c in CAMERAS],
                    "active_streams": active_count(),
                    "max_streams": MAX_STREAMS,
                    "slice_sec": SLICE_SEC,
                })

            if u.path == "/api/ping":
                return self._json({"cameras": [
                    {"name": c["name"], "ip": c["ip"],
                     "alive": probe_alive(c["ip"])} for c in CAMERAS]})

            if u.path == "/api/sysinfo":
                return self._json(sys_info())

            # ---------- 静态资源(本地 hls.js 与 logo, 不依赖公网 CDN) ----------
            if u.path.startswith("/static/"):
                fn = os.path.basename(u.path)
                # 白名单, 避免任意文件读取
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
                return self._send_file(p, ctype,
                                       "public, max-age=86400")

            # ---------- 录像查询 ----------
            if u.path == "/api/overview":
                return self._json(read_overview())

            if u.path == "/api/segments":
                cam = q.get("cam", [""])[0]
                date = q.get("date", [None])[0]
                if cam not in CAM_MAP:
                    return self._err("未知摄像头: %s" % cam)
                segs = list_segments(cam, date)
                # 逐个 ffprobe 极慢(实测 50 片要 9 秒), 会把整个时间轴卡住。
                # 所以默认不 probe, 前端只对"当前选中的那一段"按需查详情。
                # ?probe=1 仅在分片数很少(<=8)时返回, 避免拖慢整页。
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
                # 路径穿越防护
                if cam not in CAM_MAP or not os.path.isfile(p) \
                        or os.path.dirname(os.path.realpath(p)) != \
                        os.path.realpath(os.path.join(REC_DIR, cam)):
                    return self._err("文件不存在", 404)

                # 正在写入的分片 moov 未落盘, 强行播放必然失败。
                # 判据与 list_segments 一致: 是该路最新分片且仍在增长。
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
                        # 注意: 所有 header 必须在写 body 之前 send 完,
                        # 否则 BaseHTTPRequestHandler 的缓冲区会把 body 混进响应头, 导致连接中断
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
    # 先采一次基线, 否则首次请求拿不到 CPU 占用(需要两次采样做差)
    sys_info()
    threading.Thread(target=reaper_loop, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    log("服务启动 :%d (实时预览并发上限 %d)" % (a.port, MAX_STREAMS))
    print("NVR service on http://%s:%d" % (a.host, a.port))
    srv.serve_forever()


if __name__ == "__main__":
    main()
