#!/usr/bin/env bash
# 一键部署到 Linux 服务器（阿里云轻量 / ECS，Ubuntu 或 Debian）。
#
#   bash deploy/setup.sh
#
# 幂等：重复执行只更新依赖和服务，不会重复问已经填好的东西。
set -euo pipefail
# 这个脚本一路上会写出凭据文件、日志和快照，里面有学号、姓名和全部成绩。
# 默认 umask 022 会让它们变成 0644，同机器上任何账号都能读。
umask 077

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=jwgrade
ENV_FILE="/etc/${SERVICE}.env"
UNIT_FILE="/etc/systemd/system/${SERVICE}.service"
VENV="${PROJECT_DIR}/.venv"
REQ_FILE="${PROJECT_DIR}/requirements.lock.txt"
USE_LOCK=1
if [[ ! -f "$REQ_FILE" ]]; then
  REQ_FILE="${PROJECT_DIR}/requirements.txt"
  USE_LOCK=0
fi
# 服务不该以 root 跑。这里只做默认值。
SVC_USER=jwgrade

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

# 密码和 token 均不回显，只显示字符数量；token 还会检查基本格式。
# 这样可以发现输入不完整等问题，同时避免凭据出现在终端截图中。
say_len() { printf '  已记录：%d 个字符\n' "${#1}"; }

# 密码：只回读长度
read_password() {
  read -rsp "$1" JW_PASSWORD; echo
  say_len "$JW_PASSWORD"
}

# PushPlus token 通常是 32 位十六进制；格式不符时询问是否重新输入。
read_token() {
  local t retry
  while :; do
    read -rsp "$1" t; echo
    if [[ -z "$t" ]]; then
      echo "  没有输入"
      break
    fi
    printf '  已记录：%d 个字符' "${#t}"
    if [[ "$t" =~ ^[0-9a-fA-F]{32}$ ]]; then
      echo "，格式符合（32 位十六进制）"
      break
    fi
    echo "。PushPlus token 通常是 32 位十六进制，当前输入格式不符合。"
    read -rp "  要重新输入吗？[Y/n]: " retry
    if [[ "$retry" =~ ^[Nn]$ ]]; then break; fi
  done
  PUSHPLUS_TOKEN="$t"
}

command -v apt-get >/dev/null || {
  echo "本脚本按 Ubuntu/Debian 写的。其它发行版请把 apt-get 换成 yum/dnf。"; exit 1; }

# 单元里开了 ProtectHome=true，systemd 会把 /home、/root、/run/user 整个
# 变成服务看不见的空目录。项目装在这几处的话，WorkingDirectory 和 .venv
# 里的 python 都会凭空消失，而报错只是一句 status=200/CHDIR，很难联想到这儿。
# （/root 另外还有 700 权限那一层，非特权用户本来也进不去。）
case "$PROJECT_DIR" in
  /root/*|/home/*|/run/user/*)
    echo "项目不能放在 $PROJECT_DIR：服务单元开了 ProtectHome=true，"
    echo "/home、/root、/run/user 对服务是不可见的。先搬到 /opt 再装："
    echo "  sudo mv $PROJECT_DIR /opt/jwgrade && cd /opt/jwgrade && bash deploy/setup.sh"
    exit 1;;
esac

id "$SVC_USER" &>/dev/null || sudo useradd --system --no-create-home   --shell /usr/sbin/nologin "$SVC_USER"

# ---------------------------------------------------------------- 时区
# 云服务器默认常是 UTC。配置里的 quiet_hours 按本地时间判断，时区不对的话
# "凌晨1-7点不查"会变成"上午9点到下午3点不查"，正好错过出分时段。
say "设置时区为 Asia/Shanghai"
sudo timedatectl set-timezone Asia/Shanghai
date

# ---------------------------------------------------------------- 依赖
say "安装系统依赖"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip

# ---------------------------------------------------------------- Python 版本
# src/config.py 顶层 `from itertools import pairwise` 要 3.10+。不在这儿拦的话，
# apt / venv / pip 三步可能全部成功，但运行检查仍会因 ImportError 失败，容易被误判为程序
# 坏了，其实是发行版自带的 python3 太老。Ubuntu 20.04 是 3.8，Debian 11 是 3.9。
PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo
  echo "本机的 python3 是 ${PY_VER}，本项目需要 3.10 或更高。"
  echo
  echo "Ubuntu 20.04 自带 3.8、Debian 11 自带 3.9，都装不起来。"
  echo "请换成 Ubuntu 22.04 及以上，或 Debian 12 及以上，再重跑本脚本。"
  echo
  exit 21
fi
say "Python ${PY_VER}，满足 3.10+ 要求"

say "创建虚拟环境并安装 Python 依赖"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
if [[ "$USE_LOCK" -eq 1 ]]; then
  "$VENV/bin/pip" install -q --no-deps -r "$REQ_FILE"
else
  "$VENV/bin/pip" install -q -r "$REQ_FILE"
fi
"$VENV/bin/pip" check
# 脚本开头的 umask=077 会让新建 venv 变成 root/安装者独占。systemd
# 之后以 jwgrade 运行，若不显式补组权限，服务启动时连解释器都读不了。
sudo chown -R root:"$SVC_USER" "$VENV"
sudo chmod -R g=rX,o= "$VENV"
mkdir -p "${PROJECT_DIR}/data"

# ---------------------------------------------------------------- 配置
if [[ ! -f "${PROJECT_DIR}/config.yaml" ]]; then
  say "生成 config.yaml"
  cp "${PROJECT_DIR}/config.example.yaml" "${PROJECT_DIR}/config.yaml"
  read -rp "请输入学号: " STUDENT_ID
  sed -i "s/^  username: .*/  username: \"${STUDENT_ID}\"/" "${PROJECT_DIR}/config.yaml"
  echo "已写入学号。轮询频率等参数随时可以改 config.yaml，保存即生效。"
else
  echo "config.yaml 已存在，跳过"
fi
sudo chown root:"$SVC_USER" "${PROJECT_DIR}/config.yaml"
sudo chmod 640 "${PROJECT_DIR}/config.yaml"
sudo mkdir -p "${PROJECT_DIR}/data"
sudo chown -R "$SVC_USER":"$SVC_USER" "${PROJECT_DIR}/data"
DATA_DIR="${PROJECT_DIR}/data"
# 一次性权限迁移：程序现在会用 0600 建这些文件，但那只对新建的生效。
# 早先在 UMask=0022 下跑出来的 run.log / history.jsonl 会一直是 0644，
# 里面是成绩流水，同机器上任何账号都读得到。
for f in grades.json history.jsonl pending.json run.log last_push.marker login_blocked jwgrade.lock login_rate.json session.json; do
  [[ -e "${DATA_DIR}/$f" ]] && sudo chmod 600 "${DATA_DIR}/$f"
done
true


# ---------------------------------------------------------------- 凭据
# 服务器没有桌面环境，密钥链后端不可用，所以走环境变量文件。
# root 可写、jwgrade 组只读（640），且不经过命令行参数，
# 不会出现在 shell 历史或 ps 输出里。
if [[ ! -f "$ENV_FILE" ]]; then
  say "配置凭据（输入时不显示，没有就直接回车跳过）"
  read_password "教务系统密码: "
  read_token "PushPlus token（没有就直接回车跳过）: "
  # printf 而非 heredoc：值里的 $ 和反引号不会被展开
  printf 'JW_PASSWORD=%s\nPUSHPLUS_TOKEN=%s\n' "$JW_PASSWORD" "$PUSHPLUS_TOKEN" \
    | sudo tee "$ENV_FILE" >/dev/null
  sudo chmod 640 "$ENV_FILE"
  sudo chown root:"$SVC_USER" "$ENV_FILE"
else
  # 不静默跳过：改了教务密码的人重跑本脚本，就是为了换掉它。
  # 旧提示要求用户删除整个凭据文件，容易误操作，删除后还要重新输入
  # PushPlus token。这里只更新教务密码，并保留现有 token。
  echo "凭据文件 $ENV_FILE 已存在。"
  read -rp "要重新输入教务密码吗？（在教务系统改过密码就选 y）[y/N]: " CHANGE_PW
  if [[ "$CHANGE_PW" =~ ^[Yy]$ ]]; then
    read_password "教务系统密码: "
    # 只换密码，PushPlus token 原样保留
    ENV_KEEP="$(sudo grep -v '^JW_PASSWORD=' "$ENV_FILE" || true)"
    {
      printf 'JW_PASSWORD=%s\n' "$JW_PASSWORD"
      if [[ -n "$ENV_KEEP" ]]; then printf '%s\n' "$ENV_KEEP"; fi
    } | sudo tee "$ENV_FILE" >/dev/null
    sudo chmod 640 "$ENV_FILE"
    sudo chown root:"$SVC_USER" "$ENV_FILE"
    echo "已更新密码，PushPlus token 保持不变。"
  else
    echo "保持不变。"
  fi

  # token 为空时重新询问。config.yaml 里 pushplus.enabled 默认是 true，
  # 空 token 会导致配置检查失败，因此不能直接沿用。
  if ! sudo grep -qE '^PUSHPLUS_TOKEN=.+' "$ENV_FILE"; then
    echo
    echo "凭据文件里的 PushPlus token 是空的。"
    read_token "PushPlus token（直接回车则关闭推送）: "
    ENV_KEEP="$(sudo grep -v '^PUSHPLUS_TOKEN=' "$ENV_FILE" || true)"
    {
      if [[ -n "$ENV_KEEP" ]]; then printf '%s\n' "$ENV_KEEP"; fi
      printf 'PUSHPLUS_TOKEN=%s\n' "$PUSHPLUS_TOKEN"
    } | sudo tee "$ENV_FILE" >/dev/null
    sudo chmod 640 "$ENV_FILE"
    sudo chown root:"$SVC_USER" "$ENV_FILE"
  fi
fi

# 上面的提示写着「没有就直接回车跳过」，那就得让跳过真的能跑通：
# 空 token + enabled: true 会让配置校验直接退 21，装机走不完。
# config.example.yaml 里 enabled 只有 pushplus 这一处，缩进四格，所以能这么锚。
if sudo grep -qE '^PUSHPLUS_TOKEN=.+' "$ENV_FILE"; then
  if grep -qE '^    enabled: false' "${PROJECT_DIR}/config.yaml"; then
    sudo sed -i 's/^    enabled: false/    enabled: true/' "${PROJECT_DIR}/config.yaml"
    echo "检测到 PushPlus token，已把 notify.pushplus.enabled 设回 true。"
  fi
else
  sudo sed -i 's/^    enabled: true/    enabled: false/' "${PROJECT_DIR}/config.yaml"
  cat <<EOF

没有 PushPlus token，已把 config.yaml 的 notify.pushplus.enabled 设为 false。
监控照常运行，只是不会推送到微信。以后想开推送：
  1) 把 PUSHPLUS_TOKEN=你的token 写进 ${ENV_FILE}
  2) 把 config.yaml 里 pushplus 段的 enabled 改回 true
  3) sudo systemctl restart ${SERVICE}
或者直接重跑本脚本，它会问你要 token。
EOF
fi

# ---------------------------------------------------------------- 服务
say "安装 systemd 服务"
sudo tee "$UNIT_FILE" >/dev/null <<EOF
[Unit]
Description=教务成绩自动监控
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV}/bin/python -m src.main
# on-failure + RestartPreventExitStatus：程序遇到密码错误会主动退出（码 20）
# 以避免重复登录导致账号被锁定。使用 Restart=always 会让 systemd 每 60 秒
# 重新尝试一次，从而绕过这项保护。
Restart=on-failure
RestartSec=60
RestartPreventExitStatus=20 21

# 整个文件系统只读，只放开自己的 data/ 目录
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
RestrictSUIDSGID=true
RestrictNamespaces=true
LockPersonality=true
MemoryDenyWriteExecute=true
RestrictAddressFamilies=AF_INET AF_INET6
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
UMask=0077
ReadWritePaths=${PROJECT_DIR}/data

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload

# ---------------------------------------------------------------- 部署检查
# 首次部署时密码尚未验证，因此部署检查使用临时配置副本，最多允许三次输入。
# 正式 config.yaml 不会被修改，服务启动后仍使用默认的一小时一次、一天一次限制。
# 使用临时副本可以避免脚本被中断后把宽松配置遗留在正式文件中。
SETUP_CFG="${PROJECT_DIR}/.config.setup.yaml"
trap 'sudo rm -f "$SETUP_CFG"' EXIT

sudo cp "${PROJECT_DIR}/config.yaml" "$SETUP_CFG"
# 提交密码的那一轮会往限速账本记**两笔**（换票探测一笔、提交密码一笔），
# 而小时闸不分类型全都数。所以填 5 才等于 3 次尝试：
#   第 1 次 账本 0 < 5 放行 → 变 2 笔
#   第 2 次 账本 2 < 5 放行 → 变 4 笔
#   第 3 次 账本 4 < 5 放行 → 变 6 笔
#   第 4 次 账本 6 ≥ 5 拦住
#
# 日闸抬到 9（只数 password 那一类，所以就是 9 次），目的是**让小时闸成为
# 唯一约束**。填 3 的话：三次用完时先报错的是小时闸，它说「约 60 分钟后可
# 再试」——可等满一小时之后日闸又拦住，说「约 23 小时」。那句 60 分钟没说
# 谎，但它后面还站着一堵没预告的墙，人白等一小时。
#
# 抬到 9 之后每一句「60 分钟」都是真的：3 次 → 冷却一小时 → 再 3 次 →
# 一天封顶 9 次。这个节奏也更贴近 CAS 账号锁定真正在意的东西（短时间内
# 连续失败），而不是让人一次烧完三次再锁一整天。
#
# 每天九次只用于人工完成首次部署。临时副本删除后，限制会恢复为 1 / 1。
sudo sed -i 's/^  max_logins_per_hour: .*/  max_logins_per_hour: 5/' "$SETUP_CFG"
sudo sed -i 's/^  max_password_logins_per_day: .*/  max_password_logins_per_day: 9/' "$SETUP_CFG"
sudo chown root:"$SVC_USER" "$SETUP_CFG"
sudo chmod 640 "$SETUP_CFG"

# 上一次登录失败会留下阻断标记，保留该标记会使部署检查直接失败。LoginBlocked
# 映射到退出码 21，下面的重试循环只认 20，所以那种失败连重试都不会触发。
#
# 这里无条件跑一次解锁：没有标记时它是空操作；有标记且密码确实换过就解开；
# 有标记但密码没变会失败——那正是该停下来的情况，不是该重试的情况。
say "确认没有遗留的登录阻断"
set +e
sudo systemd-run --quiet --wait --collect --pipe \
  --unit="${SERVICE}-preunlock-$$" \
  --uid="$SVC_USER" --gid="$SVC_USER" \
  --working-directory="$PROJECT_DIR" \
  --property="EnvironmentFile=$ENV_FILE" \
  --property="ReadWritePaths=$DATA_DIR" \
  "$VENV/bin/python" -m src.main --unlock-login --config "$SETUP_CFG"
PREUNLOCK=$?
set -e
if [[ $PREUNLOCK -ne 0 ]]; then
  cat <<EOF

上次登录失败留下的阻断标记解不开——当前密码和上次失败时用的是同一个。

如果你刚在教务系统改过密码，重跑本脚本并在「要重新输入教务密码吗」那一步选 y。
如果没改过，先去 https://ids.chd.edu.cn 手动登录确认正确的密码。
EOF
  exit $PREUNLOCK
fi

say "检查登录状态目录"
# 预检故意查正式配置，不查副本——要确认的是服务将来真正会用的那一份
sudo systemd-run --quiet --wait --collect --pipe \
  --unit="${SERVICE}-preflight-$$" \
  --uid="$SVC_USER" --gid="$SVC_USER" \
  --working-directory="$PROJECT_DIR" \
  --property="EnvironmentFile=$ENV_FILE" \
  --property="ReadWritePaths=$DATA_DIR" \
  "$VENV/bin/python" -m src.main --preflight --config "$PROJECT_DIR/config.yaml"

say "先跑一次，确认能登录并解析出成绩"
SMOKE_ATTEMPTS=3
DONE_TRIES=0
# 必须在循环外初始化：循环可能因 RESULT -ne 20 提前 break，此时该变量
# 尚未赋值，而后面的收尾逻辑要读取它——脚本启用了 set -u，会直接报错。
BLOCK_STATE=""
for (( TRY = 1; TRY <= SMOKE_ATTEMPTS; TRY++ )); do
  DONE_TRIES=$TRY
  set +e
  # 不带 --dump：部署检查只需确认流程是否正常，无需在磁盘上保留
  # 含姓名、学号和全部成绩的原始 HTML。解析出问题时再手动加 --dump。
  # 直接让 systemd 解析 EnvironmentFile，避免这里手写的 shell 解析规则
  # 和正式服务对引号、反斜杠、# 等特殊字符的解释不一致。--pipe 只把输出
  # 传回当前终端，不会把凭据放进命令行参数。
  sudo systemd-run --quiet --wait --collect --pipe \
    --unit="${SERVICE}-smoke${TRY}-$$" \
    --uid="$SVC_USER" --gid="$SVC_USER" \
    --working-directory="$PROJECT_DIR" \
    --property="EnvironmentFile=$ENV_FILE" \
    --property="ReadWritePaths=$DATA_DIR" \
    "$VENV/bin/python" -m src.main --once --config "$SETUP_CFG"
  RESULT=$?
  set -e
  # 确保已有状态文件统一归服务用户所有，兼容旧版本或人工运行留下的文件。
  sudo chown -R "$SVC_USER":"$SVC_USER" "${PROJECT_DIR}/data"

  if [[ $RESULT -eq 0 ]]; then
    break
  fi
  # 只有密码错误适合重新输入。配置错误或页面结构变化无法通过重复尝试解决。
  if [[ $RESULT -ne 20 ]]; then
    break
  fi

  # 退出码 20 涵盖两类失败，二者的恢复方式相反：
  #   blocked  已确认密码错误，需要更换密码
  #   human    需要验证码或账号被临时锁定，**更换密码没有作用**，
  #            必须由人在网页端处理一次
  # 退出码无法区分（LoginNeedsHuman 是 LoginFailed 的子类，同为 20），
  # 但登录阻断标记里记录了 state。据此给出对应的提示，避免在验证码
  # 场景下反复要求重新输入密码。
  if [[ -f "${DATA_DIR}/login_blocked" ]]; then
    BLOCK_STATE="$(sudo "$VENV/bin/python" -c 'import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        print(json.load(f).get("state", ""))
except Exception:
    print("")' "${DATA_DIR}/login_blocked" 2>/dev/null || true)"
  fi
  if [[ "$BLOCK_STATE" == "human" ]]; then
    cat <<EOF

本次失败**不是密码错误**，因此不会要求重新输入密码，重新输入也无法解决。

原因是认证服务器要求图形验证码，或账号被临时锁定。这两种情况都需要人工处理：
请打开 https://ids.chd.edu.cn 手动登录一次（通过验证码，或等待锁定解除），
确认可以正常登录后，重新运行本脚本。
EOF
    break
  fi

  if [[ $TRY -eq $SMOKE_ATTEMPTS ]]; then
    break
  fi

  echo
  echo "登录未通过，可能是密码输入错误。还可以再试 $(( SMOKE_ATTEMPTS - TRY )) 次。"
  read_password "重新输入教务系统密码: "
  # 只更新密码，PushPlus token 保持不变，避免重复输入。
  ENV_KEEP="$(sudo grep -v '^JW_PASSWORD=' "$ENV_FILE" || true)"
  {
    printf 'JW_PASSWORD=%s\n' "$JW_PASSWORD"
    if [[ -n "$ENV_KEEP" ]]; then printf '%s\n' "$ENV_KEEP"; fi
  } | sudo tee "$ENV_FILE" >/dev/null
  sudo chmod 640 "$ENV_FILE"
  sudo chown root:"$SVC_USER" "$ENV_FILE"

  # 上一次失败留下了阻断标记，不解除的话下一轮连试都不会试。
  # 这里没有绕过任何保险：--unlock-login 自己会比对密码是不是真的变了，
  # 没变就拒绝。脚本只是替你把命令敲了，检查照常执行。
  say "解除上次失败留下的登录阻断"
  set +e
  sudo systemd-run --quiet --wait --collect --pipe \
    --unit="${SERVICE}-unlock${TRY}-$$" \
    --uid="$SVC_USER" --gid="$SVC_USER" \
    --working-directory="$PROJECT_DIR" \
    --property="EnvironmentFile=$ENV_FILE" \
    --property="ReadWritePaths=$DATA_DIR" \
    "$VENV/bin/python" -m src.main --unlock-login --config "$SETUP_CFG"
  UNLOCKED=$?
  set -e
  if [[ $UNLOCKED -ne 0 ]]; then
    cat <<EOF

解除登录阻断失败，当前输入可能与上次失败时使用的密码相同。
先去 https://ids.chd.edu.cn 手动登录确认正确的密码，再重跑本脚本。
EOF
    exit $UNLOCKED
  fi
done

if [[ $RESULT -ne 0 ]]; then
  cat <<EOF

首次运行失败（退出码 $RESULT），服务未启动。先排查上面的报错。
如果是解析问题，加 --dump 重跑一次可把原始页面存到 ${PROJECT_DIR}/data/dump_*.html（包含姓名、学号和全部成绩；查看后请及时删除）。
EOF
  if [[ $RESULT -eq 20 && "$BLOCK_STATE" == "human" ]]; then
    cat <<EOF
上面的失败是验证码或账号锁定，**不是密码错误**。请在网页端处理后重新运行本脚本。
EOF
  elif [[ $RESULT -eq 20 ]]; then
    # 说 $DONE_TRIES 而不是上限：真被小时闸提前拦住时，实际试的次数会更少，
    # 避免报告一个实际没有发生的次数。
    cat <<EOF
本轮 ${DONE_TRIES} 次密码尝试均未通过。请先前往 https://ids.chd.edu.cn 手动确认密码。
确认后再重新运行本脚本。当前一小时内的尝试次数已用完，请等待约一小时。
每天最多允许尝试 9 次；达到上限后，请等待次日额度恢复。
EOF
  fi
  exit $RESULT
fi

say "启动常驻服务"
# 部署检查通过后才登记开机自启。如果提前登记，检查失败并退出时，
# 可能留下当前未运行、但下次开机自动启动的状态，并在无人关注时发起认证。
# 因此，只有部署检查成功后才启用开机自启。
sudo systemctl enable "$SERVICE" >/dev/null
sudo systemctl restart "$SERVICE"
sleep 2
sudo systemctl status "$SERVICE" --no-pager -l | head -12

cat <<EOF

部署完成。常用命令：

  sudo systemctl status ${SERVICE}                        查看状态
  sudo journalctl -u ${SERVICE} -f                        实时日志
  cd ${PROJECT_DIR} && sudo -u ${SVC_USER} .venv/bin/python -m src.main --history 20
                                                          看变更历史
  （cd 不能省：-m src.main 按当前目录找 src 包，sudo -u 不改工作目录）

改轮询频率直接编辑 ${PROJECT_DIR}/config.yaml，保存即生效，不用重启。
服务异常时先查看状态和日志，不要反复重启，以免重新触发认证。
EOF
