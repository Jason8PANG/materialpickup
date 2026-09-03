#!/bin/bash
# ================================================================
# 挂载 Bartender 打印共享目录（Windows SMB → Linux 本地路径）
# 用途：物料领取看板（docker）Bartender 触发文件输出目录
#   Windows 共享: \\172.26.1.7\Coil_Label_Scanned
#   Linux 挂载点: /mnt/coil_label_scanned（docker-compose volumes 已映射）
# ================================================================
set -e

SHARE='//172.26.1.7/Coil_Label_Scanned'
MOUNT_POINT='/mnt/coil_label_scanned'

# 凭据：填写真实 Windows 域账号（建议改用专用只写账号）
SMB_USER='NAI-GROUP\\jasonadmin'
SMB_PASS='<CHANGE_ME>'

echo "==> 检查 cifs-utils"
command -v mount.cifs >/dev/null 2>&1 || { echo "缺少 cifs-utils，安装: yum install -y cifs-utils"; exit 1; }

echo "==> 创建挂载点"
mkdir -p "$MOUNT_POINT"

echo "==> 写入凭据文件（权限 600）"
CREDS='/etc/cifs-creds-coil'
umask 077
cat > "$CREDS" <<EOF
username=$SMB_USER
password=$SMB_PASS
domain=NAI-GROUP
EOF
chmod 600 "$CREDS"

echo "==> 挂载 $SHARE -> $MOUNT_POINT"
if mountpoint -q "$MOUNT_POINT"; then
    echo "已挂载，跳过"
else
    mount -t cifs "$SHARE" "$MOUNT_POINT" \
        -o "credentials=$CREDS,vers=3.0,file_mode=0666,dir_mode=0777,nobrl"
fi

echo "==> 验证可写"
TEST_FILE="$MOUNT_POINT/_write_test_$(date +%s).tmp"
if touch "$TEST_FILE" 2>/dev/null; then
    rm -f "$TEST_FILE"
    echo "OK：共享可写"
else
    echo "ERROR：无法写入共享，检查网络/账号权限"
    exit 1
fi

echo "==> 建议加入 /etc/fstab 持久化："
echo "//172.26.1.7/Coil_Label_Scanned  /mnt/coil_label_scanned  cifs  credentials=/etc/cifs-creds-coil,vers=3.0,file_mode=0666,dir_mode=0777,nobrl  0  0"
echo "完成。"
