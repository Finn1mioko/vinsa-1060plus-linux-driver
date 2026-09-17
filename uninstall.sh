#!/usr/bin/env bash
set -euo pipefail

APP_NAME="vinsa-t501"
APP_DIR="/opt/${APP_NAME}"
BIN_PATH="/usr/local/bin/${APP_NAME}"
SERVICE_NAME="${APP_NAME}.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
UDEV_PATH="/etc/udev/rules.d/70-${APP_NAME}.rules"

if [[ "${EUID}" -ne 0 ]]; then
    echo "Ошибка: удаление нужно выполнять от root."
    echo "Используйте: sudo ./uninstall.sh"
    exit 1
fi

echo "==> Удаление VINSA T501 driver"

echo "==> Остановка сервиса..."
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true

echo "==> Отключение сервиса..."
systemctl disable "${SERVICE_NAME}" 2>/dev/null || true

echo "==> Удаление systemd service..."
rm -f "${SERVICE_PATH}"

systemctl daemon-reload

echo "==> Удаление udev rule..."
rm -f "${UDEV_PATH}"

udevadm control --reload-rules 2>/dev/null || true
udevadm trigger 2>/dev/null || true

echo "==> Удаление launcher..."
rm -f "${BIN_PATH}"

echo "==> Удаление файлов драйвера..."
rm -rf "${APP_DIR}"

echo
echo "VINSA T501 driver удалён."
echo
echo "Модуль uinput специально НЕ выгружается,"
echo "поскольку он может использоваться другими программами."
```
