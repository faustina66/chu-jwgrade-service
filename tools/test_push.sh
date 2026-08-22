#!/usr/bin/env bash
set -euo pipefail

# 部署完成后的安全冒烟测试：只推送一条虚构成绩，不访问教务系统。
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${JWGRADE_SERVICE_USER:-jwgrade}"
ENV_FILE="${JWGRADE_ENV_FILE:-/etc/jwgrade.env}"

if [[ ! -f "${PROJECT_DIR}/config.yaml" ]]; then
  echo "找不到 ${PROJECT_DIR}/config.yaml，请先完成部署。" >&2
  exit 2
fi
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "找不到 ${ENV_FILE}，请先完成部署。" >&2
  exit 2
fi
if [[ ! -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
  echo "找不到 Python 虚拟环境，请先完成部署。" >&2
  exit 2
fi

if [[ "${EUID}" -eq 0 ]]; then
  SUDO=()
else
  SUDO=(sudo)
fi

"${SUDO[@]}" systemd-run --quiet --wait --collect --pipe \
  --unit="jwgrade-push-test-$$" \
  --uid="${SERVICE_USER}" --gid="${SERVICE_USER}" \
  --working-directory="${PROJECT_DIR}" \
  --property="EnvironmentFile=${ENV_FILE}" \
  --property="ReadWritePaths=${PROJECT_DIR}/data" \
  "${PROJECT_DIR}/.venv/bin/python" \
  "${PROJECT_DIR}/tools/demo_push.py" \
  --config "${PROJECT_DIR}/config.yaml" \
  --push --only 1 \
  --out "${PROJECT_DIR}/data/push-preview.html"

echo
echo "测试完成：请查看微信中的 PushPlus 测试消息。"
echo "HTML 预览已生成：${PROJECT_DIR}/data/push-preview.html"
