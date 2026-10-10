#!/bin/bash
# 日志看门狗
#
# 背景: 这台板子根分区(eMMC)只有 15G，而 /etc/logrotate.d/rsyslog 原配置是
# "rotate 4 + weekly" —— 最坏情况 4 份 x 3.3G = 13G 日志能把根分区撑爆。
# 2026-10-02 就因为 motion 对 6 路离线摄像头每 3 秒刷一次重连日志，
# 写出 2785 万行 / 3.26G，系统在 20:55 彻底卡死。
#
# 作用: 定期检查根分区，剩余不足 1.5G 时清掉最大的日志文件。
# 保留 .guard-<时间戳> 一份，便于事后排查；下次轮转会自动清掉。

LOGDIR=/var/log
MIN_AVAIL_MB=1500      # 剩余低于此值才动手
BIG_FILE_MB=100        # 只处理超过此大小的单个日志
GUARD_DIR=/var/log/.guard-archive   # 归档目录(放大盘更稳妥, 这里留在同盘但改名为 .log 便于 logrotate 忽略)

used=$(df -Pm / | awk 'NR==2{print $3}')
avail=$(df -Pm / | awk 'NR==2{print $4}')

# 空间充足就直接退出，不做任何事
if [ "$avail" -ge "$MIN_AVAIL_MB" ]; then
    exit 0
fi

big=$(find "$LOGDIR" -maxdepth 2 -type f -name "*.log" -size +${BIG_FILE_MB}M 2>/dev/null | head -1)
if [ -z "$big" ]; then
    # 没有大文件就压缩最大的旧日志
    big=$(find "$LOGDIR" -maxdepth 2 -type f -name "*.log.[0-9]*" -size +${BIG_FILE_MB}M 2>/dev/null | head -1)
    if [ -z "$big" ]; then
        logger -t log-guard "根分区剩 ${avail}M, 但没有超过 ${BIG_FILE_MB}M 的日志, 需人工检查"
        exit 0
    fi
fi

ts=$(date +%Y%m%d-%H%M%S)
size=$(du -h "$big" | cut -f1)
logger -t log-guard "根分区剩 ${avail}M, 清理大日志 $big ($size)"

# 保留头部 2000 行供排查，主体清空
if [ -w "$big" ]; then
    head -2000 "$big" > "${big}.guard-${ts}" 2>/dev/null
    : > "$big"
else
    rm -f "$big"
fi

logger -t log-guard "完成, 当前剩余 $(df -Pm / | awk 'NR==2{print $4}')M"
