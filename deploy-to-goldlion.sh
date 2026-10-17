#!/bin/bash
# ============================================================
# 部署 nvr-monitor 到校区 RK3399 板子
#   ./deploy-to-goldlion.sh
# 仓库里是脱敏模板(192.168.1.x / YOUR_CAM_PASSWORD),
# 本脚本把真实配置灌入后上传, 板端自动备份旧文件,
# 重启 nvr-web 并打印验证结果。录像服务(nvr-recorder)不动。
#
# 真实配置放在 deploy.local.conf(已 gitignore, 不会入库),
# 首次使用: cp deploy.local.conf.example deploy.local.conf 后填真实值。
# ============================================================
set -e
cd "$(dirname "$0")"

# ---------- 真实配置(优先取本地 deploy.local.conf, 否则用环境变量, 再不行用占位符) ----------
BOARD="192.168.1.20"                    # 板子 IP(占位)
KEY="$HOME/.ssh/id_ed25519"             # SSH 私钥(占位)
LAN_SEG="192.168.1"                     # 校区网段前三段(占位)
CAM_PASS="YOUR_CAM_PASSWORD"            # 需认证摄像头的密码(占位)
SITE_NAME="NVR 监控服务"                 # 服务端标题(占位)
SITE_TITLE="校区 · 监控中心"             # 页面标题
SITE_DESC="SparkMinds 创智实验室 监控中心"
BRAND_ALT="SparkMinds 创智实验室"

[ -f deploy.local.conf ] && . ./deploy.local.conf

SSHOPT="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o IdentitiesOnly=yes -o ConnectTimeout=10"
STAMP=$(date +%Y%m%d-%H%M)
STAGE="/tmp/nvr-deploy-$STAMP"
mkdir -p "$STAGE"

echo "==> 1/5 灌入真实配置(仓库保持脱敏模板): 板子=$BOARD 网段=$LAN_SEG.x"
python3 - "$LAN_SEG" "$CAM_PASS" "$SITE_NAME" "$SITE_TITLE" "$SITE_DESC" "$BRAND_ALT" <<'PYEOF'
import sys
seg, cam_pass, site_name, title, desc, alt = sys.argv[1:7]

# nvr-server.py: 相机 IP 段 + 密码 + 标题
s = open('nvr-server.py').read()
s = s.replace('192.168.1.', seg + '.')
s = s.replace('YOUR_CAM_PASSWORD', cam_pass)
s = s.replace('NVR 监控服务 (示例配置)', site_name)
open('/tmp/nvr-deploy-stage-server.py', 'w').write(s)

# index.html: 标题/描述/logo/页脚 IP
s = open('index.html').read()
s = s.replace('<title>NVR 监控中心</title>', '<title>%s</title>' % title)
s = s.replace('<meta name="description" content="NVR 监控中心 — 基于 ARM 单板机的轻量监控录像与实时预览">',
              '<meta name="description" content="%s">' % desc)
s = s.replace('    <div class="brand">\n      \n      <span class="brand-div"></span>',
              '    <div class="brand">\n      <img class="brand-logo" src="/static/logo-light.png" alt="%s">\n      <span class="brand-div"></span>' % alt)
s = s.replace('<h1>NVR 监控中心</h1>', '<h1>%s</h1>' % title)
s = s.replace('192.168.1.20', seg + '.20')
open('/tmp/nvr-deploy-stage-index.html', 'w').write(s)
print('    substituted ok')
PYEOF
cp /tmp/nvr-deploy-stage-server.py "$STAGE/nvr-server.py"
cp /tmp/nvr-deploy-stage-index.html "$STAGE/index.html"

echo "==> 2/5 板端备份现有文件(.bak-$STAMP)"
ssh -i "$KEY" $SSHOPT "root@$BOARD" "
  cp /opt/nvr/bin/nvr-server.py /opt/nvr/bin/nvr-server.py.bak-$STAMP
  cp /opt/nvr/bin/index.html    /opt/nvr/bin/index.html.bak-$STAMP
  echo '    backup ok'"

echo "==> 3/5 上传新文件 + 诊断脚本"
scp -O -i "$KEY" $SSHOPT "$STAGE/nvr-server.py" "root@$BOARD:/opt/nvr/bin/nvr-server.py"
scp -O -i "$KEY" $SSHOPT "$STAGE/index.html"    "root@$BOARD:/opt/nvr/bin/index.html"
scp -O -i "$KEY" $SSHOPT probe-cameras.sh       "root@$BOARD:/opt/nvr/bin/probe-cameras.sh"
ssh -i "$KEY" $SSHOPT "root@$BOARD" "chmod +x /opt/nvr/bin/probe-cameras.sh; echo '    upload ok'"

echo "==> 4/5 语法检查 + 重启 nvr-web(录像服务不受影响)"
ssh -i "$KEY" $SSHOPT "root@$BOARD" "
  python3 -m py_compile /opt/nvr/bin/nvr-server.py || { echo '语法错误, 回滚!'; cp /opt/nvr/bin/nvr-server.py.bak-$STAMP /opt/nvr/bin/nvr-server.py; exit 1; }
  systemctl restart nvr-web
  sleep 4
  systemctl is-active nvr-web"

echo "==> 5/5 验证: 等后台探测完成(约 40 秒)后看各路模式"
sleep 40
ssh -i "$KEY" $SSHOPT "root@$BOARD" "
  echo '--- /api/live 各路模式 ---'
  curl -s http://127.0.0.1/api/live | python3 -c \"
import json,sys
d=json.load(sys.stdin)
for c in d['cameras']:
    print('  %-9s %-6s %-8s streaming=%s' % (c['name'], c.get('codec'), c.get('mode'), c['streaming']))
print('  在放:', d['stream_modes'], '| 上限: 软编%s 硬编%s 总%s' % (d['max_streams'], d['max_hw_streams'], d['max_total']))
print('  板子硬编:', '可用' if d['caps']['hw_enc'] else '不可用(自动走软编)')
\"
  echo '--- live.log 尾部 ---'
  tail -5 /mnt/nvr/logs/live.log"

echo
echo "部署完成。回滚命令(如需):"
echo "  ssh -i $KEY root@$BOARD 'cp /opt/nvr/bin/nvr-server.py.bak-$STAMP /opt/nvr/bin/nvr-server.py; cp /opt/nvr/bin/index.html.bak-$STAMP /opt/nvr/bin/index.html; systemctl restart nvr-web'"
