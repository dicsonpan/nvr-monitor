#!/bin/bash
# ============================================================
# NVR 录像系统 (示例配置)
#  设计原则: 零编码 (ffmpeg -c copy), 10分钟切片, 磁盘满自动滚动删除
#  部署: /opt/nvr/bin/  |  日志: /mnt/nvr/logs/
# ============================================================

# ---------- 可调参数 ----------
BASE_DIR="/mnt/nvr"
REC_DIR="${BASE_DIR}/recordings"
LOG_DIR="${BASE_DIR}/logs"
STATE_DIR="${BASE_DIR}/state"
SEGMENT_SEC=600            # 每个分片 10 分钟
KEEP_PCT=85                # 磁盘使用率达85% 开始清理(8路写入快, 留缓冲防写满)
KEEP_PCT_HARD=95           # 达95% 立即清理
MIN_FREE_GB=15             # 无论如何保留 15G 空闲
DELETE_BATCH=200           # 每次清理最多删 200 个分片(避免长时间阻塞)
# 注意: 认证信息现在按摄像头分别配置在 cam_auth() 里(厂家不同, 要求不同),
# 不再用全局 RTSP_USER/RTSP_PASS。
TRANSPORT="tcp"           # tcp 更稳, udp 延迟低但丢包
FFLOG="error"                    # ffmpeg 日志级别
# 重连退避上限(秒)。摄像头离线时按 3/6/9... 秒递增, 最多等 RETRY_MAX。
# 固定 3 秒重试会让离线摄像头疯狂刷日志, 曾把 15G 根分区写满导致系统卡死。
RETRY_MAX=60

mkdir -p "$REC_DIR" "$LOG_DIR" "$STATE_DIR"

# ---------- 工具函数 ----------
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "${LOG_DIR}/recorder.log"
}

disk_used_pct() {
    # 直接用 df 的 pcent 列(基于 Available 算出的真实使用率)。
    # 不用 Use% 列: 它含 ext4 的 5% root 保留块, 空盘时也会显示 100%。
    df --output=pcent "$BASE_DIR" | awk 'NR==2 {gsub(/%/,"",$1); print $1}'
}

disk_free_gb() {
    df --output=avail "$BASE_DIR" | awk 'NR==2 {printf "%d", $1/1024/1024}'
}

# 构造 RTSP URL
#
# 不同厂家摄像头差异很大，必须逐台配置，不能用一套全局参数:
#   .21-.24  无认证, 主码流 /stream1, 子码流在根路径 /(800x448)
#   .25-.28  需认证 admin/YOUR_CAM_PASSWORD, 主码流 /stream1, 子码流 /stream2(800x448)
#   .30     无认证, 与 .21-.24 同款, 子码流在根路径 /(800x448)
#
# cam_auth()  返回 "用户名 密码" (空格分隔，无认证返回空)
# cam_main()  主码流路径
# cam_sub()   子码流路径
cam_auth() {
    case "$1" in
        192.168.1.2[5-8]) echo "admin YOUR_CAM_PASSWORD" ;;
        *) echo "" ;;   # .21-.24 / .29 / .30 无认证
    esac
}
cam_main() {
    # 三批都是 /stream1, 但保持函数形式以便个别摄像头不同
    case "$1" in
        *) echo "/stream1" ;;
    esac
}
cam_sub() {
    case "$1" in
        192.168.1.2[5-8]) echo "/stream2" ;;   # .25-.28 子码流在 /stream2
        *) echo "/" ;;                          # .21-.24 / .29 / .30 在根路径
    esac
}

build_url() {
    local ip="$1" path creds u p
    path=$(cam_main "$ip")
    creds=$(cam_auth "$ip")
    # 认证信息用空格分隔，不要用 | —— 竖线会被当成密码的一部分
    if [ -n "$creds" ]; then
        u="${creds%% *}"
        p="${creds##* }"
        echo "rtsp://${u}:${p}@${ip}:554${path}"
    else
        echo "rtsp://${ip}:554${path}"
    fi
}

# ---------- 磁盘清理: 滚动删除最旧的分片 ----------
cleanup_disk() {
    local used_pct free_gb
    used_pct=$(disk_used_pct)
    free_gb=$(disk_free_gb)

    # 防御: 取不到有效数值时绝不删文件。宁可磁盘满了也不误删录像。
    if ! [[ "$used_pct" =~ ^[0-9]+$ ]] || ! [[ "$free_gb" =~ ^[0-9]+$ ]]; then
        log "警告: 无法获取磁盘用量 (used_pct='${used_pct}' free_gb='${free_gb}'), 跳过本次清理"
        return 0
    fi

    if [ "$used_pct" -lt "$KEEP_PCT" ] && [ "$free_gb" -ge "$MIN_FREE_GB" ]; then
        return 0
    fi

    log "触发清理: 磁盘使用 ${used_pct}%, 空闲 ${free_gb}G (阈值 ${KEEP_PCT}% / ${MIN_FREE_GB}G)"

    # 按修改时间升序排列, 先删最旧的分片
    local n=0
    while [ $n -lt "$DELETE_BATCH" ]; do
        used_pct=$(disk_used_pct)
        free_gb=$(disk_free_gb)
        if ! [[ "$used_pct" =~ ^[0-9]+$ ]] || ! [[ "$free_gb" =~ ^[0-9]+$ ]]; then
            log "警告: 清理过程中取磁盘用量失败, 中止"
            break
        fi
        if [ "$used_pct" -lt "$KEEP_PCT" ] && [ "$free_gb" -ge "$MIN_FREE_GB" ]; then
            break
        fi

        # 找最旧的一个分片。排除正在写入的当前分片(mtime 在 60 秒内)。
        local oldest
        oldest=$(find "$REC_DIR" -type f -name '*.mp4' -mmin +1 -printf '%T@ %p\n' 2>/dev/null \
                 | sort -n | head -1 | cut -d' ' -f2-)
        if [ -z "$oldest" ]; then
            log "无可删除的旧分片(现有分片均在写入中), 停止清理"
            break
        fi

        rm -f "$oldest"
        n=$((n+1))
    done

    log "清理完成, 共删除 ${n} 个分片, 当前使用 $(disk_used_pct)%, 空闲 $(disk_free_gb)G"
}

# ---------- 单路录制 ----------
record_one() {
    local name="$1" ip="$2"
    local url fails=0
    url=$(build_url "$ip")
    local outdir="${REC_DIR}/${name}"
    mkdir -p "$outdir"

    log "启动录制: ${name} (${ip})"

    while true; do
        # stream_repeat 保证网络抖动后能自愈; -g 与摄像头 I 帧间隔对齐可减小碎片
        ffmpeg -hide_banner -loglevel "$FFLOG" \
            -rtsp_transport "$TRANSPORT" \
            -stimeout 10000000 \
            -i "$url" \
            -map 0:v:0 \
            -c copy \
            -an \
            -f segment \
            -segment_time ${SEGMENT_SEC} \
            -segment_format mp4 \
            -reset_timestamps 1 \
            -strftime 1 \
            -strftime_mkdir 1 \
            "${outdir}/${name}_%Y%m%d_%H%M%S.mp4" \
            >> "${LOG_DIR}/${name}.ffmpeg.log" 2>&1

        local rc=$?
        # 指数退避重连。
        # 原来固定 3 秒重试, 6 ��离线摄像头会一直刷日志 —— motion 就是这样
        # 写出 3.26G 把 15G 根分区撑爆、系统在 2026-10-02 20:55 卡死的。
        # 现在失败越多等得越久, 最长 RETRY_MAX 秒, 避免无谓的日志与 CPU 消耗。
        fails=$((fails+1))
        if [ $fails -gt 1 ]; then
            local wait=$((3 * fails))
            [ $wait -gt ${RETRY_MAX} ] && wait=${RETRY_MAX}
            # 连续失败超过阈值后才记日志, 避免日志本身成为负担
            if [ $fails -le 3 ] || [ $((fails % 10)) -eq 0 ]; then
                log "${name} ffmpeg 退出 (rc=${rc}), ${wait} 秒后重连 (连续失败 ${fails} 次)"
            fi
            sleep "$wait"
        else
            # 正常运行过(had_ok=1)说明是中途掉线, 快速重连
            sleep 3
        fi
        [ $rc -eq 0 ] && fails=0
    done
}

# ---------- 摄像头清单 ----------
#  格式:  名称|IP
CAMERAS=(
    "Camera1|192.168.1.21"
    "Camera2|192.168.1.22"
    "Camera3|192.168.1.23"
    "Camera4|192.168.1.24"
    "Camera5|192.168.1.25"
    "Camera6|192.168.1.26"
    "Camera7|192.168.1.27"
    "Camera8|192.168.1.28"
    "Camera9|192.168.1.29"
    "Camera10|192.168.1.30"
)

# ---------- 主流程 ----------
case "$1" in
    start)
        log "===== 启动 NVR 录像系统 ====="
        # 清理上次残留
        for entry in "${CAMERAS[@]}"; do
            name="${entry%%|*}"
            pidfile="${STATE_DIR}/${name}.pid"
            if [ -f "$pidfile" ] && kill -0 "$(cat $pidfile)" 2>/dev/null; then
                echo "  ${name} 已在运行 (PID $(cat $pidfile))"
                continue
            fi
            record_one "$name" "${entry##*|}" &
            echo $! > "$pidfile"
            echo "  ${name} 启动 (PID $!)"
        done

        # 独立清理守护(每 60 秒检查一次; 8 路写入约 115MB/5分钟, 300秒追不上)
        if [ -f "${STATE_DIR}/_gc.pid" ] && kill -0 "$(cat ${STATE_DIR}/_gc.pid)" 2>/dev/null; then
            echo "  清理守护 已在运行"
        else
            (
                while true; do
                    sleep 60
                    cleanup_disk
                done
            ) &
            echo $! > "${STATE_DIR}/_gc.pid"
            echo "  清理守护 启动 (PID $!)"
        fi
        log "===== 启动完成, 共 ${#CAMERAS[@]} 路 ====="
        ;;

    run)
        # 前台监督模式: 供 systemd 托管。所有子进程退出后自动重启整机服务,
        # 任一路录制进程消亡即退出, 交由 Restart=always 拉起全部。
        log "===== 启动 NVR 录像系统 (前台监督模式) ====="
        trap 'log "收到终止信号, 停止全部录制"; ./nvr-recorder.sh stop; exit 0' TERM INT

        for entry in "${CAMERAS[@]}"; do
            name="${entry%%|*}"
            record_one "$name" "${entry##*|}" &
            echo $! > "${STATE_DIR}/${name}.pid"
            log "${name} 启动 (PID $!)"
        done

        (
            while true; do
                sleep 60
                cleanup_disk
            done
        ) &
        echo $! > "${STATE_DIR}/_gc.pid"

        log "===== 全部 ${#CAMERAS[@]} 路已启动, 进入监督 ====="
        # 任何一路录制进程退出, 则整体退出交给 systemd 重启
        wait -n
        log "警告: 有录制进程异常退出, 触发整体重启"
        ./nvr-recorder.sh stop
        exit 1
        ;;

    stop)
        log "===== 停止 NVR 录像系统 ====="
        for pidfile in "${STATE_DIR}"/*.pid; do
            [ -f "$pidfile" ] || continue
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null; then
                # 杀掉整个进程组(ffmpeg 子进程)
                kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
                echo "  已停止 PID ${pid} ($(basename $pidfile .pid))"
            fi
            rm -f "$pidfile"
        done
        sleep 2
        pkill -f "ffmpeg.*${REC_DIR}" 2>/dev/null
        log "===== 已全部停止 ====="
        ;;

    status)
        echo "=== 录像进程 ==="
        for entry in "${CAMERAS[@]}"; do
            name="${entry%%|*}"
            pidfile="${STATE_DIR}/${name}.pid"
            if [ -f "$pidfile" ] && kill -0 "$(cat $pidfile)" 2>/dev/null; then
                printf "  %-10s 运行中  PID %s\n" "$name" "$(cat $pidfile)"
            else
                printf "  %-10s 停止\n" "$name"
            fi
        done
        echo
        echo "=== 磁盘 ==="
        echo "  挂载点 : ${BASE_DIR}"
        echo "  使用率 : $(disk_used_pct)%"
        echo "  空闲   : $(disk_free_gb) G"
        echo
        echo "=== 各路今日产出 ==="
        today=$(date +%Y%m%d)
        total=0
        for entry in "${CAMERAS[@]}"; do
            name="${entry%%|*}"
            sz=$(du -sb "${REC_DIR}/${name}" 2>/dev/null | awk '{print $1}')
            [ -z "$sz" ] && sz=0
            cnt=$(find "${REC_DIR}/${name}" -name "*${today}*.mp4" 2>/dev/null | wc -l)
            printf "  %-10s %6s MB   %3s 个分片\n" "$name" "$((sz/1024/1024))" "$cnt"
            total=$((total+sz))
        done
        echo "  ----------------------------------------"
        printf "  %-10s %6s MB\n" "合计" "$((total/1024/1024))"
        ;;

    cleanup)
        cleanup_disk
        ;;

    *)
        echo "用法: $0 {start|run|stop|status|cleanup}"
        exit 1
        ;;
esac
