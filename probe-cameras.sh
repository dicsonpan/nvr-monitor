#!/bin/bash
# ============================================================
# NVR 摄像头子码流诊断脚本(在板子上跑)
#   ./probe-cameras.sh
# 逐台探测子码流编码/分辨率/帧率, 检测板子硬编能力,
# 并给出每路的建议预览模式(直通/硬编/软编)。
# 只读探测, 不改任何摄像头或服务配置。
#
# 部署时把下面三个变量改成你的实际值(或用 deploy.local.conf 注入):
#   LAN_SEG   校区网段前三段, 如 192.168.1
#   AUTH_IP_PATTERN  需要认证的摄像头 IP 模式(bash case 语法)
#   AUTH_USER / AUTH_PASS  认证用户名/密码
# ============================================================
BASE_DIR="/mnt/nvr"
LOG_DIR="${BASE_DIR}/logs"
mkdir -p "$LOG_DIR"

LAN_SEG="${NVR_LAN_SEG:-192.168.1}"
AUTH_IP_PATTERN="${NVR_AUTH_IP_PATTERN:-192.168.1.2[5-8]}"
AUTH_USER="${NVR_AUTH_USER:-admin}"
AUTH_PASS="${NVR_AUTH_PASS:-YOUR_CAM_PASSWORD}"

auth_of() { case "$1" in ${AUTH_IP_PATTERN}) echo "${AUTH_USER} ${AUTH_PASS}";; *) echo "";; esac; }
sub_of()  { case "$1" in ${AUTH_IP_PATTERN}) echo "/stream2";; *) echo "/";; esac; }

CAMS=(${LAN_SEG}.21 ${LAN_SEG}.22 ${LAN_SEG}.23 ${LAN_SEG}.24
      ${LAN_SEG}.25 ${LAN_SEG}.26 ${LAN_SEG}.27 ${LAN_SEG}.28
      ${LAN_SEG}.29 ${LAN_SEG}.30)

echo "========== 板子能力 =========="
ffmpeg -version | head -1
if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_v4l2m2m; then
  # 列出来不代表能用(RK3399  multiplanar 节点老 ffmpeg 会挂), 实测一条
  if timeout 20 ffmpeg -hide_banner -loglevel error -f lavfi \
      -i "testsrc=size=640x360:rate=10:duration=1" \
      -c:v h264_v4l2m2m -f null - >/dev/null 2>&1; then
    echo "VPU 硬编(h264_v4l2m2m): 可用"
  else
    echo "VPU 硬编(h264_v4l2m2m): 不可用(设备在但驱动/ffmpeg 不支持, 需升级 ffmpeg>=5.1)"
  fi
else
  echo "VPU 硬编(h264_v4l2m2m): ffmpeg 未编译该编码器"
fi
ffmpeg -hide_banner -decoders 2>/dev/null | grep -q hevc_v4l2m2m \
  && echo "HEVC 硬解(hevc_v4l2m2m): 有" || echo "HEVC 硬解(hevc_v4l2m2m): 无"
echo

echo "========== 子码流探测 =========="
printf "%-8s %-16s %-8s %-10s %-8s %s\n" "摄像头" "子码流编码" "分辨率" "帧率" "建议" "说明"
n_h264=0; n_hevc=0; n_fail=0
for ip in "${CAMS[@]}"; do
  creds=$(auth_of "$ip"); path=$(sub_of "$ip")
  if [ -n "$creds" ]; then
    u="${creds%% *}"; p="${creds##* }"
    url="rtsp://${u}:${p}@${ip}:554${path}"
  else
    url="rtsp://${ip}:554${path}"
  fi
  r=$(timeout 10 ffprobe -hide_banner -loglevel error \
        -rtsp_transport tcp -stimeout 5000000 \
        -select_streams v:0 \
        -show_entries stream=codec_name,width,height,avg_frame_rate \
        -of csv=p=0 "$url" 2>/dev/null | head -1)
  if [ -z "$r" ]; then
    printf "%-8s %-16s %-8s %-10s %-8s %s\n" "$ip" "探测失败" "-" "-" "软编" "摄像头离线或路径/认证不对"
    n_fail=$((n_fail+1)); continue
  fi
  codec=$(echo "$r" | cut -d, -f1)
  wh=$(echo "$r" | cut -d, -f2,3)
  fps=$(echo "$r" | cut -d, -f4 | cut -d/ -f1)
  case "$codec" in
    h264) mode="直通"; note="零转码 remux, CPU≈0"; n_h264=$((n_h264+1));;
    hevc) mode="软编"; note="需转码(或相机端改 H.264 后即可直通)"; n_hevc=$((n_hevc+1));;
    *)    mode="软编"; note="非标准编码, 走转码"; n_hevc=$((n_hevc+1));;
  esac
  printf "%-8s %-16s %-8s %-10s %-8s %s\n" "$ip" "$codec" "$wh" "${fps}fps" "$mode" "$note"
done
echo
echo "汇总: H.264 ${n_h264} 路(可直通) / 需转码 ${n_hevc} 路 / 失败 ${n_fail} 路"
echo "提示: 相机端把子码流编码改成 H.264 后, 在板子上跑一次:"
echo "  curl -s http://localhost/api/reprobe   # 或 systemctl restart nvr-web"
